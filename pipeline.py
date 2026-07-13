from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import math
import os
import shutil
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from config import (
    DEFAULT_ACTIVE_ROW,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_CROP_BOTTOM_RATIO,
    DEFAULT_FPS,
    DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE,
    DEFAULT_MIN_LINE_GAP_MS,
    DEFAULT_OFFSET_MS,
    DEFAULT_RAW_OCR_TEXT_SCORE,
    DEFAULT_SAME_THRESHOLD,
    DEFAULT_STYLE,
    DEFAULT_SWITCH_CONFIRM_FRAMES,
    DEFAULT_WORKERS,
)
from detector import detect_line_switches
from events import (
    BestEffortEventSink,
    PipelineCancelled,
    check_cancellation,
    emit_event,
    terminal_event_type,
)
from extractor import (
    create_roi_preview,
    ensure_media_tools,
    extract_frames,
    probe_video,
    resolve_roi,
)
from lrc_writer import write_lrc
from ocr import ocr_all_frames
from utils import atomic_write_json, text_variants


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent


def _default_work_root(*, frozen: bool | None = None) -> Path:
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return PROJECT_ROOT / "work"
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "Video2LRC" / "work"


DEFAULT_WORK_ROOT = _default_work_root()
DATA_FORMAT_VERSION = 3
DETECTOR_ALGORITHM_VERSION = 8


class PipelineError(RuntimeError):
    """Raised when a pipeline stage cannot complete."""


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    video_path: Path
    output_path: Path | None = None
    work_root: Path | None = None
    fps: float = DEFAULT_FPS
    workers: int = DEFAULT_WORKERS
    keep_workdir: bool = False
    resume: bool = False
    style: str = DEFAULT_STYLE
    crop_bottom_ratio: float | None = None
    roi: tuple[float, float, float, float] | None = None
    preview_roi: bool = False
    active_row: str = DEFAULT_ACTIVE_ROW
    offset_ms: int = DEFAULT_OFFSET_MS
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    same_threshold: float = DEFAULT_SAME_THRESHOLD
    switch_confirm_frames: int = DEFAULT_SWITCH_CONFIRM_FRAMES
    min_line_gap_ms: int = DEFAULT_MIN_LINE_GAP_MS
    max_blank_frames_inside_line: int = DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE
    dry_run: bool = False
    force: bool = False
    strict: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Unsupported cache-key value: {type(value)!r}")


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fingerprint_file(
    path: str | Path,
    chunk_size: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    """Return metadata plus a complete SHA-256 content fingerprint."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Video file does not exist: {source}")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least one byte")

    stat = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    final_stat = source.stat()
    if (final_stat.st_size, final_stat.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise PipelineError(f"Video changed while it was being fingerprinted: {source}")

    return {
        "path": str(source),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _dependency_versions(names: Iterable[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _is_link_or_reparse(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point/junction."""

    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _reserve_run_dir(work_root: Path, base_id: str, resume: bool) -> Path:
    base = work_root / base_id
    if resume:
        candidates: list[Path] = []
        for path in work_root.glob(f"{base_id}*"):
            if path.name != base_id and not path.name.startswith(f"{base_id}-"):
                continue
            if _is_link_or_reparse(path):
                raise PipelineError(f"Refusing to resume a linked run cache: {path}")
            if path.is_dir():
                candidates.append(path)
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
        base.mkdir(parents=False, exist_ok=False)
        return base

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for counter in range(10_000):
        candidate = base if counter == 0 else work_root / f"{base_id}-{stamp}-{counter}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise PipelineError(f"Cannot reserve a unique run directory under {work_root}")


def _acquire_run_lock(run_dir: Path) -> Path:
    if _is_link_or_reparse(run_dir):
        raise PipelineError(f"Refusing to lock a linked run cache: {run_dir}")
    lock_path = run_dir / ".run.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PipelineError(
            f"Run cache is already locked: {run_dir}. Another process may be using it; "
            "remove .run.lock only after confirming that process has stopped."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(f"pid={os.getpid()}\nstarted_at={_utc_now()}\n")
    except Exception:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return lock_path


def _release_run_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Cannot read cache file {path}: {exc}") from exc


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"data_format_version": DATA_FORMAT_VERSION, "stages": {}}
    data = _read_json(path)
    if not isinstance(data, dict):
        raise PipelineError(f"Invalid manifest root in {path}")
    if data.get("data_format_version") != DATA_FORMAT_VERSION:
        return {"data_format_version": DATA_FORMAT_VERSION, "stages": {}}
    data.setdefault("stages", {})
    return data


def _cache_hit(
    manifest: Mapping[str, Any],
    stage: str,
    key: str,
    artifacts: Sequence[Path],
) -> bool:
    stage_data = manifest.get("stages", {}).get(stage, {})
    return stage_data.get("key") == key and all(path.exists() for path in artifacts)


def _record_stage(
    manifest: dict[str, Any],
    manifest_path: Path,
    stage: str,
    key: str,
    artifacts: Sequence[Path],
    *,
    reused: bool = False,
    details: Mapping[str, Any] | None = None,
) -> None:
    stages = manifest.setdefault("stages", {})
    previous = stages.get(stage, {})
    now = _utc_now()
    stage_details = dict(details) if details is not None else dict(previous.get("details", {}))
    stages[stage] = {
        "key": key,
        "completed_at": previous.get("completed_at", now) if reused else now,
        "last_reused_at": now if reused else None,
        "reuse_count": int(previous.get("reuse_count", 0)) + (1 if reused else 0),
        "artifacts": [str(path) for path in artifacts],
        "reused": reused,
        "details": stage_details,
    }
    atomic_write_json(manifest_path, manifest)


def _invalidate_stages(
    manifest: dict[str, Any],
    manifest_path: Path,
    *stage_names: str,
) -> None:
    stages = manifest.setdefault("stages", {})
    changed = False
    for stage_name in stage_names:
        if stage_name in stages:
            stages.pop(stage_name)
            changed = True
    if changed:
        manifest["updated_at"] = _utc_now()
        atomic_write_json(manifest_path, manifest)


def _clean_ocr_results(frames: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for source in frames:
        frame = dict(source)
        variants = text_variants(str(frame.get("text", "")))
        frame.update(variants)
        cleaned.append(frame)
    return cleaned


def _coerce_ocr_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PipelineError("OCR cache must contain a list")
    if any(not isinstance(frame, Mapping) for frame in value):
        raise PipelineError("OCR cache entries must be objects")
    return [dict(frame) for frame in value]


def _coerce_detector_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("lines"), list):
        raise PipelineError("detector cache must contain an object with a lines list")
    if any(not isinstance(line, Mapping) for line in value["lines"]):
        raise PipelineError("detector cache lines must be objects")
    return dict(value)


def _ocr_stats(frames: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "frame_count": len(frames),
        "failed_frame_count": sum(frame.get("status") == "error" for frame in frames),
        "usable_text_frame_count": sum(bool(str(frame.get("text", "")).strip()) for frame in frames),
    }


def _validate_ocr_results(frames: Sequence[Mapping[str, Any]], cache_path: Path) -> None:
    stats = _ocr_stats(frames)
    if stats["frame_count"] == 0:
        raise PipelineError(f"OCR received no extracted frames; inspect {cache_path.parent}")
    if stats["failed_frame_count"] == stats["frame_count"]:
        raise PipelineError(f"OCR failed for every frame; inspect failure details in {cache_path}")
    if stats["usable_text_frame_count"] == 0:
        raise PipelineError(
            "OCR found no usable subtitle text; adjust --crop-bottom-ratio/--roi, "
            f"raise --fps, or inspect {cache_path}"
        )


def _validate_detector_lines(result: Mapping[str, Any], cache_path: Path) -> None:
    if not result.get("lines"):
        raise PipelineError(
            "No stable subtitle lines were detected; adjust ROI/FPS/confidence settings "
            f"and inspect {cache_path}"
        )


def _extract_frame_index(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        for key in ("frame_index", "frames"):
            value = result.get(key)
            if isinstance(value, list):
                return [dict(item) if isinstance(item, Mapping) else {"path": str(item)} for item in value]
    if isinstance(result, list):
        return [dict(item) if isinstance(item, Mapping) else {"path": str(item)} for item in result]
    raise PipelineError("extract_frames returned no frame index")


def _validate_frame_index(
    value: Any,
    frames_dir: Path,
    *,
    require_files: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise PipelineError("frame index must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise PipelineError("frame index entries must be objects")
        raw_name = item.get("frame") or item.get("path")
        if not raw_name:
            raise PipelineError("frame index entry is missing frame/path")
        name = Path(str(raw_name)).name
        if len(Path(name).stem) != 6 or not Path(name).stem.isdigit() or Path(name).suffix.lower() != ".png":
            raise PipelineError(f"invalid frame filename in cache: {name}")
        if name in names:
            raise PipelineError(f"duplicate frame in cache: {name}")
        try:
            timestamp = float(item["time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(f"invalid timestamp for cached frame: {name}") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise PipelineError(f"invalid timestamp for cached frame: {name}")

        expected_path = (frames_dir / name).resolve()
        if require_files and not expected_path.is_file():
            raise PipelineError(f"cached frame is missing: {expected_path}")
        entry = dict(item)
        entry.update(
            {
                "frame": name,
                "path": str(expected_path),
                "relative_path": f"frames/{name}",
                "time": timestamp,
            }
        )
        normalized.append(entry)
        names.add(name)

    if require_files:
        actual_names = {
            path.name
            for path in frames_dir.glob("*.png")
            if len(path.stem) == 6 and path.stem.isdigit()
        }
        if actual_names != names:
            raise PipelineError("cached frame directory and frame_index.json do not match")
    return normalized


def _validate_options(options: PipelineOptions) -> None:
    if not math.isfinite(float(options.fps)) or options.fps <= 0:
        raise ValueError("fps must be greater than zero")
    if not isinstance(options.workers, int) or isinstance(options.workers, bool) or options.workers < 1:
        raise ValueError("workers must be at least one")
    if options.roi is not None and options.crop_bottom_ratio is not None:
        raise ValueError("roi and crop_bottom_ratio are mutually exclusive")
    if options.roi is not None:
        if len(options.roi) != 4 or not all(math.isfinite(float(item)) for item in options.roi):
            raise ValueError("roi must contain four finite numbers")
        x, y, width, height = options.roi
        if min(options.roi) < 0 or max(options.roi) > 1 or width <= 0 or height <= 0:
            raise ValueError("roi coordinates must be in [0, 1] with positive width/height")
        if x + width > 1 or y + height > 1:
            raise ValueError("roi must stay inside the normalized frame")
    if options.crop_bottom_ratio is not None and (
        not math.isfinite(float(options.crop_bottom_ratio))
        or not 0 < options.crop_bottom_ratio <= 1
    ):
        raise ValueError("crop_bottom_ratio must be in (0, 1]")
    if not math.isfinite(float(options.confidence_threshold)) or not 0 <= options.confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be in [0, 1]")
    if not math.isfinite(float(options.same_threshold)) or not 0 <= options.same_threshold <= 100:
        raise ValueError("same_threshold must be in [0, 100]")
    if (
        not isinstance(options.switch_confirm_frames, int)
        or isinstance(options.switch_confirm_frames, bool)
        or options.switch_confirm_frames < 1
    ):
        raise ValueError("switch_confirm_frames must be at least one")
    if (
        not isinstance(options.max_blank_frames_inside_line, int)
        or isinstance(options.max_blank_frames_inside_line, bool)
        or options.max_blank_frames_inside_line < 0
    ):
        raise ValueError("max_blank_frames_inside_line cannot be negative")
    if (
        not isinstance(options.min_line_gap_ms, int)
        or isinstance(options.min_line_gap_ms, bool)
        or options.min_line_gap_ms < 0
    ):
        raise ValueError("min_line_gap_ms cannot be negative")
    if not isinstance(options.offset_ms, int) or isinstance(options.offset_ms, bool):
        raise ValueError("offset_ms must be an integer")


def _safe_cleanup_frames(run_dir: Path, work_root: Path, frames_dir: Path) -> bool:
    """Delete only the known frames directory inside a reserved run directory."""

    if _is_link_or_reparse(run_dir) or _is_link_or_reparse(frames_dir):
        raise PipelineError(f"Refusing to clean a linked run or frames directory: {frames_dir}")
    resolved_root = work_root.resolve()
    resolved_run = run_dir.resolve()
    resolved_frames = frames_dir.resolve()
    if resolved_run == resolved_root or not _is_relative_to(resolved_run, resolved_root):
        raise PipelineError(f"Refusing to clean frames outside the work root: {run_dir}")
    if resolved_frames != (resolved_run / "frames").resolve():
        raise PipelineError(f"Refusing to clean unexpected frames path: {frames_dir}")
    if resolved_frames == resolved_root or not _is_relative_to(
        resolved_frames, resolved_root
    ):
        raise PipelineError(f"Refusing to clean unsafe frames path: {frames_dir}")
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
        return True
    return False


def _validate_output_path(video_path: Path, output_path: Path, work_root: Path) -> None:
    resolved_output = output_path.resolve()
    if resolved_output == video_path.resolve():
        raise ValueError("output_path must not be the input video path")
    if _is_relative_to(resolved_output, work_root):
        raise ValueError("output_path must be outside the internal work cache directory")


def _run_pipeline(
    options: PipelineOptions,
    events: Any = None,
    cancellation: Any = None,
) -> dict[str, Any]:
    _validate_options(options)
    video_path = Path(options.video_path).expanduser().resolve()
    fingerprint = fingerprint_file(video_path)

    work_root = Path(options.work_root or DEFAULT_WORK_ROOT).expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    output_path = Path(options.output_path).expanduser().resolve() if options.output_path else video_path.with_suffix(".lrc")
    _validate_output_path(video_path, output_path, work_root)
    if not options.preview_roi and not options.dry_run and not options.force and output_path.exists():
        raise PipelineError(f"output already exists: {output_path}; use --force to overwrite")

    run_id = fingerprint["sha256"][:16]
    run_dir = _reserve_run_dir(work_root, run_id, options.resume)
    frames_dir = run_dir / "frames"
    manifest_path = run_dir / "manifest.json"
    frame_index_path = run_dir / "frame_index.json"
    ocr_raw_path = run_dir / "ocr_raw.json"
    ocr_clean_path = run_dir / "ocr_clean.json"
    lines_path = run_dir / "lines.json"
    review_path = run_dir / "review.csv"

    lock_path = _acquire_run_lock(run_dir)
    try:
        manifest = _load_manifest(manifest_path) if options.resume else {
            "data_format_version": DATA_FORMAT_VERSION,
            "stages": {},
        }
        for terminal_field in (
            "cancellation",
            "error",
            "output",
            "would_write_output",
            "frames_cleaned",
        ):
            manifest.pop(terminal_field, None)
        manifest.update(
            {
                "status": "running",
                "updated_at": _utc_now(),
                "attempt": int(manifest.get("attempt", 0)) + 1,
                "run_id": run_id,
                "run_dir": str(run_dir),
                "input": fingerprint,
                "options": {key: _json_default(value) if isinstance(value, (Path, tuple)) else value for key, value in asdict(options).items()},
            }
        )
        atomic_write_json(manifest_path, manifest)
    except Exception:
        _release_run_lock(lock_path)
        raise

    def stage_started(stage: str, **details: Any) -> None:
        check_cancellation(cancellation)
        emit_event(events, "stage_started", stage=stage, **details)

    def stage_completed(
        stage: str,
        *,
        reused: bool = False,
        honor_cancellation: bool = True,
        **details: Any,
    ) -> None:
        emit_event(
            events,
            "stage_completed",
            stage=stage,
            reused=reused,
            details=details,
        )
        if honor_cancellation:
            check_cancellation(cancellation)

    def artifact(kind: str, path: Path) -> None:
        emit_event(events, "artifact", kind=kind, path=str(path))

    try:
        stage_started("prepare")
        media_tools = ensure_media_tools(ffmpeg=options.ffmpeg, ffprobe=options.ffprobe)
        video_info = probe_video(video_path, ffprobe=options.ffprobe)
        resolved_roi = resolve_roi(
            video_info,
            roi=options.roi,
            crop_bottom_ratio=options.crop_bottom_ratio,
        )
        stage_completed("prepare", video=video_info, roi=resolved_roi)

        extract_key = _stable_hash(
            {
                "version": DATA_FORMAT_VERSION,
                "input": fingerprint,
                "video": {
                    key: video_info.get(key)
                    for key in ("width", "height", "rotation", "start_time", "duration")
                },
                "roi": resolved_roi,
                "fps": options.fps,
                "ffmpeg": media_tools,
            }
        )

        if options.preview_roi:
            preview_path = run_dir / "roi_preview.jpg"
            stage_started("preview")
            _invalidate_stages(manifest, manifest_path, "preview")
            preview_result = create_roi_preview(
                video_path,
                preview_path,
                roi=options.roi,
                crop_bottom_ratio=options.crop_bottom_ratio,
                video_info=video_info,
                ffprobe=options.ffprobe,
            )
            preview_key = _stable_hash({"extract": extract_key, "preview": True})
            _record_stage(
                manifest,
                manifest_path,
                "preview",
                preview_key,
                [preview_path],
                details=preview_result if isinstance(preview_result, Mapping) else None,
            )
            stage_completed(
                "preview",
                **(dict(preview_result) if isinstance(preview_result, Mapping) else {}),
            )
            manifest["status"] = "preview_complete"
            manifest["updated_at"] = _utc_now()
            atomic_write_json(manifest_path, manifest)
            result = {
                "status": "preview_complete",
                "run_dir": str(run_dir),
                "manifest_path": str(manifest_path),
                "preview_path": str(preview_path),
                "video": video_info,
                "roi": resolved_roi,
            }
            artifact("roi_preview", preview_path)
            artifact("manifest", manifest_path)
            emit_event(events, "completed", result=result)
            return result

        versions = _dependency_versions(["rapidocr", "onnxruntime", "opencv-python", "rapidfuzz"])
        ocr_engine_kwargs = {
            "Global.text_score": min(
                DEFAULT_RAW_OCR_TEXT_SCORE,
                options.confidence_threshold,
            )
        }
        ocr_key = _stable_hash(
            {
                "extract": extract_key,
                "versions": versions,
                "confidence_threshold": options.confidence_threshold,
                "engine_kwargs": ocr_engine_kwargs,
                "active_row": options.active_row,
                "adapter_version": DATA_FORMAT_VERSION,
            }
        )
        detector_key = _stable_hash(
            {
                "ocr": ocr_key,
                "style": options.style,
                "same_threshold": options.same_threshold,
                "switch_confirm_frames": options.switch_confirm_frames,
                "min_line_gap_ms": options.min_line_gap_ms,
                "max_blank_frames_inside_line": options.max_blank_frames_inside_line,
                "detector_version": DETECTOR_ALGORITHM_VERSION,
            }
        )

        detector_result: dict[str, Any] | None = None
        if options.resume and _cache_hit(manifest, "detector", detector_key, [lines_path]):
            try:
                detector_result = _coerce_detector_result(_read_json(lines_path))
            except PipelineError as exc:
                LOGGER.warning("Ignoring invalid detector cache: %s", exc)
            if detector_result is not None:
                stage_started("detector", reuse_candidate=True)
                try:
                    _validate_detector_lines(detector_result, lines_path)
                except PipelineError:
                    _invalidate_stages(manifest, manifest_path, "detector", "writer")
                    raise
                _record_stage(
                    manifest,
                    manifest_path,
                    "detector",
                    detector_key,
                    [lines_path],
                    reused=True,
                )
                stage_completed(
                    "detector",
                    reused=True,
                    line_count=len(detector_result.get("lines", [])),
                )
                LOGGER.info("Reused detector cache: %s", lines_path)
        if detector_result is None:
            clean_results: list[dict[str, Any]]
            raw_results: list[dict[str, Any]] | None = None
            if options.resume and _cache_hit(manifest, "ocr", ocr_key, [ocr_raw_path]):
                try:
                    raw_results = _coerce_ocr_results(_read_json(ocr_raw_path))
                except PipelineError as exc:
                    LOGGER.warning("Ignoring invalid OCR cache: %s", exc)
                if raw_results is not None:
                    stage_started("ocr", reuse_candidate=True)
                    clean_results = _clean_ocr_results(raw_results)
                    atomic_write_json(ocr_clean_path, clean_results)
                    try:
                        _validate_ocr_results(raw_results, ocr_raw_path)
                    except PipelineError:
                        _invalidate_stages(
                            manifest,
                            manifest_path,
                            "ocr",
                            "detector",
                            "writer",
                        )
                        raise
                    _record_stage(
                        manifest,
                        manifest_path,
                        "ocr",
                        ocr_key,
                        [ocr_raw_path, ocr_clean_path],
                        reused=True,
                        details=_ocr_stats(raw_results),
                    )
                    stage_completed("ocr", reused=True, **_ocr_stats(raw_results))
                    LOGGER.info("Reused OCR cache: %s", ocr_raw_path)
            if raw_results is None:
                stage_started("extract")
                frame_index: list[dict[str, Any]] | None = None
                if options.resume and _cache_hit(manifest, "extract", extract_key, [frame_index_path, frames_dir]):
                    try:
                        frame_index = _validate_frame_index(
                            _read_json(frame_index_path),
                            frames_dir,
                        )
                    except PipelineError as exc:
                        LOGGER.warning("Ignoring invalid extraction cache: %s", exc)
                if frame_index is not None:
                    _record_stage(
                        manifest,
                        manifest_path,
                        "extract",
                        extract_key,
                        [frame_index_path, frames_dir],
                        reused=True,
                    )
                    stage_completed(
                        "extract",
                        reused=True,
                        frame_count=len(frame_index),
                    )
                    LOGGER.info("Reused extracted frames: %s", frames_dir)
                else:
                    _invalidate_stages(
                        manifest,
                        manifest_path,
                        "extract",
                        "ocr",
                        "detector",
                        "writer",
                    )
                    frames_dir.mkdir(parents=True, exist_ok=True)
                    extraction = extract_frames(
                        video_path,
                        frames_dir,
                        roi=options.roi,
                        fps=options.fps,
                        crop_bottom_ratio=options.crop_bottom_ratio,
                        ffmpeg=options.ffmpeg,
                        ffprobe=options.ffprobe,
                        video_info=video_info,
                        cancellation_callback=(
                            (lambda: check_cancellation(cancellation))
                            if cancellation is not None
                            else None
                        ),
                    )
                    frame_index = _validate_frame_index(
                        _extract_frame_index(extraction),
                        frames_dir,
                    )
                    atomic_write_json(frame_index_path, frame_index)
                    _record_stage(
                        manifest,
                        manifest_path,
                        "extract",
                        extract_key,
                        [frame_index_path, frames_dir],
                        details={"frame_count": len(frame_index)},
                    )
                    stage_completed(
                        "extract",
                        frame_count=len(frame_index),
                    )

                _invalidate_stages(
                    manifest,
                    manifest_path,
                    "ocr",
                    "detector",
                    "writer",
                )

                def on_ocr_progress(
                    current: int,
                    total: int,
                    frame_result: Mapping[str, Any],
                ) -> None:
                    emit_event(
                        events,
                        "progress",
                        stage="ocr",
                        current=current,
                        total=total,
                        ratio=(current / total) if total else 1.0,
                        percent=(current / total * 100.0) if total else 100.0,
                        status=str(frame_result.get("status", "unknown")),
                    )
                    check_cancellation(cancellation)

                stage_started("ocr")
                raw_results = _coerce_ocr_results(
                    ocr_all_frames(
                        frame_index,
                        workers=options.workers,
                        fps=options.fps,
                        engine_kwargs=ocr_engine_kwargs,
                        confidence_threshold=options.confidence_threshold,
                        active_row=options.active_row,
                        progress_callback=on_ocr_progress,
                        cancellation_callback=(
                            (lambda: check_cancellation(cancellation))
                            if cancellation is not None
                            else None
                        ),
                    )
                )
                clean_results = _clean_ocr_results(raw_results)
                atomic_write_json(ocr_raw_path, raw_results)
                atomic_write_json(ocr_clean_path, clean_results)
                _validate_ocr_results(raw_results, ocr_raw_path)
                _record_stage(
                    manifest,
                    manifest_path,
                    "ocr",
                    ocr_key,
                    [ocr_raw_path, ocr_clean_path],
                    details={
                        **_ocr_stats(raw_results),
                        "versions": versions,
                        "engine_kwargs": ocr_engine_kwargs,
                    },
                )
                stage_completed("ocr", **_ocr_stats(raw_results))

            _invalidate_stages(manifest, manifest_path, "detector", "writer")
            stage_started("detector")
            detector_result = _coerce_detector_result(
                detect_line_switches(
                    clean_results,
                    style=options.style,
                    confidence_threshold=options.confidence_threshold,
                    same_threshold=options.same_threshold,
                    switch_confirm_frames=options.switch_confirm_frames,
                    min_line_gap_ms=options.min_line_gap_ms,
                    max_blank_frames_inside_line=options.max_blank_frames_inside_line,
                )
            )
            atomic_write_json(lines_path, detector_result)
            _validate_detector_lines(detector_result, lines_path)
            _record_stage(
                manifest,
                manifest_path,
                "detector",
                detector_key,
                [lines_path],
                details={"line_count": len(detector_result.get("lines", []))},
            )
            stage_completed(
                "detector",
                line_count=len(detector_result.get("lines", [])),
            )

        writer_key = _stable_hash(
            {
                "detector": detector_key,
                "offset_ms": options.offset_ms,
                "format": "lrc-centiseconds-utf8-no-bom-v1",
                "strict": options.strict,
            }
        )
        _invalidate_stages(manifest, manifest_path, "writer")
        stage_started("writer", dry_run=options.dry_run)
        # A real writer invocation is the output commit point. Cancellation is
        # honored immediately before it, then a successfully committed LRC wins.
        writer_result = write_lrc(
            detector_result,
            output_path,
            offset_ms=options.offset_ms,
            force=options.force,
            strict=options.strict,
            dry_run=options.dry_run,
            review_path=review_path,
            review_force=True,
        )
        writer_summary = dict(writer_result) if isinstance(writer_result, Mapping) else {"result": writer_result}
        writer_summary.pop("lines", None)
        writer_artifacts = [] if options.dry_run else [review_path, output_path]
        _record_stage(
            manifest,
            manifest_path,
            "writer",
            writer_key,
            writer_artifacts,
            details=writer_summary,
        )
        stage_completed(
            "writer",
            honor_cancellation=options.dry_run,
            dry_run=options.dry_run,
            line_count=len(detector_result.get("lines", [])),
        )

        frames_cleaned = False
        if not options.keep_workdir and not options.dry_run:
            frames_cleaned = _safe_cleanup_frames(run_dir, work_root, frames_dir)

        manifest["status"] = "dry_run_complete" if options.dry_run else "complete"
        manifest["updated_at"] = _utc_now()
        if options.dry_run:
            manifest["would_write_output"] = str(output_path)
        else:
            manifest["output"] = str(output_path)
        manifest["frames_cleaned"] = frames_cleaned
        atomic_write_json(manifest_path, manifest)
        result = {
            "status": manifest["status"],
            "run_dir": str(run_dir),
            "manifest_path": str(manifest_path),
            "output_path": None if options.dry_run else str(output_path),
            "review_path": None if options.dry_run else (str(review_path) if review_path.exists() else None),
            "line_count": len(detector_result.get("lines", [])),
            "frames_cleaned": frames_cleaned,
            "writer": writer_summary,
        }
        artifact("lines", lines_path)
        if not options.dry_run:
            artifact("lrc", output_path)
            if review_path.exists():
                artifact("review", review_path)
        artifact("manifest", manifest_path)
        emit_event(events, "completed", result=result)
        return result
    except PipelineCancelled as exc:
        manifest["status"] = "cancelled"
        manifest["updated_at"] = _utc_now()
        manifest["cancellation"] = {"message": str(exc)}
        atomic_write_json(manifest_path, manifest)
        raise
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        manifest["updated_at"] = _utc_now()
        atomic_write_json(manifest_path, manifest)
        raise
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["updated_at"] = _utc_now()
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
        atomic_write_json(manifest_path, manifest)
        if isinstance(exc, (PipelineError, FileNotFoundError, ValueError)):
            raise
        raise PipelineError(str(exc)) from exc
    finally:
        _release_run_lock(lock_path)


def run_pipeline(
    options: PipelineOptions,
    events: Any = None,
    cancellation: Any = None,
) -> dict[str, Any]:
    """Run the pipeline with optional event delivery and cooperative cancellation."""

    event_sink = events if isinstance(events, BestEffortEventSink) else BestEffortEventSink(events)
    try:
        return _run_pipeline(options, events=event_sink, cancellation=cancellation)
    except PipelineCancelled as exc:
        if terminal_event_type(event_sink) is None:
            emit_event(event_sink, "cancelled", message=str(exc))
        raise
    except KeyboardInterrupt:
        if terminal_event_type(event_sink) is None:
            emit_event(event_sink, "cancelled", message="Interrupted")
        raise
    except Exception as exc:
        if terminal_event_type(event_sink) is None:
            reported_error = exc.__cause__ if isinstance(exc, PipelineError) else None
            if reported_error is None:
                reported_error = exc
            emit_event(
                event_sink,
                "failed",
                error={
                    "type": type(reported_error).__name__,
                    "message": str(reported_error),
                },
            )
        raise


__all__ = [
    "DATA_FORMAT_VERSION",
    "DEFAULT_WORK_ROOT",
    "PipelineCancelled",
    "PipelineError",
    "PipelineOptions",
    "fingerprint_file",
    "run_pipeline",
]
