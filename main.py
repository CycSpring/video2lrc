from __future__ import annotations

import argparse
import json
import logging
import math
import multiprocessing
import sys
from pathlib import Path
from typing import Sequence

from config import (
    ACTIVE_ROW_CHOICES,
    DEFAULT_ACTIVE_ROW,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_CROP_BOTTOM_RATIO,
    DEFAULT_FPS,
    DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE,
    DEFAULT_MIN_LINE_GAP_MS,
    DEFAULT_OFFSET_MS,
    DEFAULT_SAME_THRESHOLD,
    DEFAULT_STYLE,
    DEFAULT_SWITCH_CONFIRM_FRAMES,
    DEFAULT_WORKERS,
    STYLE_CHOICES,
)
from events import BestEffortEventSink, CancellationToken, EventEmitter, PipelineCancelled
from pipeline import PipelineError, PipelineOptions, run_pipeline
from utils import configure_utf8_stdio


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least one")
    return parsed


def _non_negative_int(value: str, name: str = "value") -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"{name} cannot be negative")
    return parsed


def _unit_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 < parsed <= 1:
        raise argparse.ArgumentTypeError("must be in (0, 1]")
    return parsed


def _confidence(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be in [0, 1]")
    return parsed


def _percentage(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be in [0, 100]")
    return parsed


def _roi(value: str) -> tuple[float, float, float, float]:
    try:
        coordinates = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must contain four comma-separated numbers") from exc
    if len(coordinates) != 4:
        raise argparse.ArgumentTypeError("must contain x,y,w,h")
    if not all(math.isfinite(item) for item in coordinates):
        raise argparse.ArgumentTypeError("coordinates must be finite numbers")
    x, y, width, height = coordinates
    if min(coordinates) < 0 or max(coordinates) > 1 or width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("coordinates must be in [0, 1] and width/height positive")
    if x + width > 1 or y + height > 1:
        raise argparse.ArgumentTypeError("ROI must stay within the normalized frame")
    return x, y, width, height


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video2lrc",
        description="Extract burned-in lyric subtitles from a video into a line-level LRC draft.",
    )
    parser.add_argument("video_path", type=Path, help="input video path")
    parser.add_argument("-o", "--output", type=Path, help="output LRC path (default: next to video)")
    parser.add_argument("--fps", type=_positive_float, default=DEFAULT_FPS, help=f"sampling FPS (default: {DEFAULT_FPS})")
    parser.add_argument("--workers", type=_positive_int, default=DEFAULT_WORKERS, help=f"OCR worker processes (default: {DEFAULT_WORKERS})")
    parser.add_argument("--workdir", type=Path, help="runtime cache root (default: project work directory)")
    parser.add_argument("--keep-workdir", action="store_true", help="keep extracted frame images after success")
    parser.add_argument("--resume", action="store_true", help="reuse compatible stage caches")
    parser.add_argument("--style", choices=STYLE_CHOICES, default=DEFAULT_STYLE, help=f"subtitle style (default: {DEFAULT_STYLE})")

    roi_group = parser.add_mutually_exclusive_group()
    roi_group.add_argument("--crop-bottom-ratio", type=_unit_float, default=None, help=f"bottom crop ratio (default: {DEFAULT_CROP_BOTTOM_RATIO})")
    roi_group.add_argument("--roi", type=_roi, help="normalized subtitle ROI: x,y,w,h")
    parser.add_argument("--preview-roi", action="store_true", help="write a multi-timestamp ROI preview and exit")
    parser.add_argument("--active-row", choices=ACTIVE_ROW_CHOICES, default=DEFAULT_ACTIVE_ROW, help=f"active row for two-line subtitles (default: {DEFAULT_ACTIVE_ROW})")
    parser.add_argument("--offset-ms", type=int, default=DEFAULT_OFFSET_MS, help="global timestamp offset in milliseconds")
    parser.add_argument("--confidence-threshold", type=_confidence, default=DEFAULT_CONFIDENCE_THRESHOLD, help="minimum OCR confidence")
    parser.add_argument("--same-threshold", type=_percentage, default=DEFAULT_SAME_THRESHOLD, help="long-line similarity threshold")
    parser.add_argument("--switch-confirm-frames", type=_positive_int, default=DEFAULT_SWITCH_CONFIRM_FRAMES, help="stable frames required to confirm a new line")
    parser.add_argument("--min-line-gap-ms", type=lambda value: _non_negative_int(value, "min-line-gap-ms"), default=DEFAULT_MIN_LINE_GAP_MS, help="gap below which a QA flag is emitted")
    parser.add_argument("--max-blank-frames-inside-line", type=lambda value: _non_negative_int(value, "max-blank-frames-inside-line"), default=DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE, help="blank frames tolerated inside a line")
    parser.add_argument("--dry-run", action="store_true", help="show a preview without writing the final LRC")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    parser.add_argument("--strict", action="store_true", help="fail on duplicate or descending rounded timestamps")
    parser.add_argument("--ffmpeg", default="ffmpeg", help=argparse.SUPPRESS)
    parser.add_argument("--ffprobe", default="ffprobe", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--event-stream",
        action="store_true",
        help="write prefixed JSONL progress events to stdout",
    )
    parser.add_argument("--job-id", help="job identifier included in event-stream records")
    parser.add_argument(
        "--cancel-file",
        type=Path,
        help="cancel cooperatively when this marker file exists",
    )
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def _print_human_result(result: dict[str, object]) -> None:
    status = result.get("status", "unknown")
    if status == "preview_complete":
        print(f"ROI preview: {result.get('preview_path')}")
        return
    print(f"Status: {status}")
    print(f"Lines: {result.get('line_count', 0)}")
    if result.get("output_path"):
        print(f"LRC: {result['output_path']}")
    if result.get("review_path"):
        print(f"Review: {result['review_path']}")
    writer = result.get("writer")
    if isinstance(writer, dict) and writer.get("preview"):
        print("Preview:")
        for line in writer["preview"]:
            print(line)
    print(f"Run cache: {result.get('run_dir')}")


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    crop_bottom_ratio = args.crop_bottom_ratio
    if crop_bottom_ratio is None and args.roi is None:
        crop_bottom_ratio = DEFAULT_CROP_BOTTOM_RATIO

    options = PipelineOptions(
        video_path=args.video_path,
        output_path=args.output,
        work_root=args.workdir,
        fps=args.fps,
        workers=args.workers,
        keep_workdir=args.keep_workdir,
        resume=args.resume,
        style=args.style,
        crop_bottom_ratio=crop_bottom_ratio,
        roi=args.roi,
        preview_roi=args.preview_roi,
        active_row=args.active_row,
        offset_ms=args.offset_ms,
        confidence_threshold=args.confidence_threshold,
        same_threshold=args.same_threshold,
        switch_confirm_frames=args.switch_confirm_frames,
        min_line_gap_ms=args.min_line_gap_ms,
        max_blank_frames_inside_line=args.max_blank_frames_inside_line,
        dry_run=args.dry_run,
        force=args.force,
        strict=args.strict,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
    )

    event_emitter = (
        BestEffortEventSink(EventEmitter(job_id=args.job_id)) if args.event_stream else None
    )
    cancellation = CancellationToken(args.cancel_file) if args.cancel_file else None
    pipeline_kwargs: dict[str, object] = {}
    if event_emitter is not None:
        pipeline_kwargs["events"] = event_emitter
    if cancellation is not None:
        pipeline_kwargs["cancellation"] = cancellation

    try:
        result = run_pipeline(options, **pipeline_kwargs)
    except PipelineCancelled as exc:
        if event_emitter is not None and event_emitter.terminal_event is None:
            event_emitter.emit("cancelled", message=str(exc))
        print(f"Cancelled: {exc}", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        if event_emitter is not None and event_emitter.terminal_event is None:
            event_emitter.emit("cancelled", message="Interrupted")
        print("Interrupted; the run cache was preserved.", file=sys.stderr)
        return 130
    except (PipelineError, OSError, ValueError) as exc:
        if event_emitter is not None and event_emitter.terminal_event is None:
            event_emitter.emit(
                "failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.event_stream:
        pass
    elif args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
