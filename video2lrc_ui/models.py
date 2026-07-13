"""Validated, shell-free command models used by the desktop UI."""

from __future__ import annotations

import math
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Sequence

from config import (
    ACTIVE_ROW_CHOICES,
    DEFAULT_ACTIVE_ROW,
    DEFAULT_CONFIDENCE_THRESHOLD,
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


PathValue = str | PathLike[str]


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _path(value: PathValue | None, name: str, *, required: bool = False) -> Path | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    try:
        text = str(value)
    except Exception as exc:  # pragma: no cover - defensive for exotic PathLike objects
        raise ValueError(f"{name} must be a path") from exc
    if not text.strip():
        raise ValueError(f"{name} is required")
    if "\x00" in text:
        raise ValueError(f"{name} cannot contain a null byte")
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError) as exc:
        raise ValueError(f"{name} must be a valid absolute path") from exc


def _format_number(value: float) -> str:
    return format(value, ".12g")


@dataclass(frozen=True, slots=True)
class ROI:
    """A normalized rectangular subtitle region."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = tuple(
            _finite_number(value, name)
            for value, name in (
                (self.x, "roi.x"),
                (self.y, "roi.y"),
                (self.width, "roi.width"),
                (self.height, "roi.height"),
            )
        )
        x, y, width, height = values
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError("ROI coordinates must be non-negative and its size must be positive")
        if x > 1 or y > 1 or width > 1 or height > 1:
            raise ValueError("ROI coordinates must be normalized to [0, 1]")
        if x + width > 1 or y + height > 1:
            raise ValueError("ROI must stay inside the normalized video frame")
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)

    @classmethod
    def parse(cls, value: str | Sequence[float]) -> "ROI":
        """Parse ``x,y,width,height`` text or a four-item sequence."""

        if isinstance(value, str):
            parts: Sequence[object] = tuple(part.strip() for part in value.split(","))
        else:
            parts = value
        if len(parts) != 4:
            raise ValueError("ROI must contain x,y,width,height")
        return cls(*(_finite_number(item, "ROI coordinate") for item in parts))

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.width, self.height

    def to_cli_value(self) -> str:
        return ",".join(_format_number(value) for value in self.as_tuple())


# A spelling-friendly alias for callers that prefer normal CamelCase.
Roi = ROI


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Validated options for one preview or extraction process."""

    video_path: PathValue
    output_path: PathValue | None = None
    workdir: PathValue | None = None
    fps: float = DEFAULT_FPS
    workers: int = DEFAULT_WORKERS
    roi: ROI | Sequence[float] | str | None = None
    crop_bottom_ratio: float | None = None
    style: str = DEFAULT_STYLE
    active_row: str = DEFAULT_ACTIVE_ROW
    offset_ms: int = DEFAULT_OFFSET_MS
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    same_threshold: float = DEFAULT_SAME_THRESHOLD
    switch_confirm_frames: int = DEFAULT_SWITCH_CONFIRM_FRAMES
    min_line_gap_ms: int = DEFAULT_MIN_LINE_GAP_MS
    max_blank_frames_inside_line: int = DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE
    keep_workdir: bool = False
    resume: bool = False
    force: bool = False
    strict: bool = False

    def __post_init__(self) -> None:
        video_path = _path(self.video_path, "video_path", required=True)
        output_path = _path(self.output_path, "output_path")
        workdir = _path(self.workdir, "workdir")

        fps = _finite_number(self.fps, "fps")
        if fps <= 0:
            raise ValueError("fps must be greater than zero")
        workers = _integer(self.workers, "workers", minimum=1)

        roi = self.roi
        if roi is not None and not isinstance(roi, ROI):
            roi = ROI.parse(roi)

        crop_bottom_ratio = self.crop_bottom_ratio
        if crop_bottom_ratio is not None:
            crop_bottom_ratio = _finite_number(crop_bottom_ratio, "crop_bottom_ratio")
            if not 0 < crop_bottom_ratio <= 1:
                raise ValueError("crop_bottom_ratio must be in (0, 1]")
        if roi is not None and crop_bottom_ratio is not None:
            raise ValueError("roi and crop_bottom_ratio are mutually exclusive")

        confidence = _finite_number(self.confidence_threshold, "confidence_threshold")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence_threshold must be in [0, 1]")
        same_threshold = _finite_number(self.same_threshold, "same_threshold")
        if not 0 <= same_threshold <= 100:
            raise ValueError("same_threshold must be in [0, 100]")

        if self.style not in STYLE_CHOICES:
            raise ValueError(f"style must be one of: {', '.join(STYLE_CHOICES)}")
        if self.active_row not in ACTIVE_ROW_CHOICES:
            raise ValueError(f"active_row must be one of: {', '.join(ACTIVE_ROW_CHOICES)}")

        offset_ms = _integer(self.offset_ms, "offset_ms")
        switch_confirm_frames = _integer(
            self.switch_confirm_frames,
            "switch_confirm_frames",
            minimum=1,
        )
        min_line_gap_ms = _integer(self.min_line_gap_ms, "min_line_gap_ms", minimum=0)
        max_blank_frames = _integer(
            self.max_blank_frames_inside_line,
            "max_blank_frames_inside_line",
            minimum=0,
        )
        for value, name in (
            (self.keep_workdir, "keep_workdir"),
            (self.resume, "resume"),
            (self.force, "force"),
            (self.strict, "strict"),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")

        if output_path is not None and output_path == video_path:
            raise ValueError("output_path must not be the input video path")

        object.__setattr__(self, "video_path", video_path)
        object.__setattr__(self, "output_path", output_path)
        object.__setattr__(self, "workdir", workdir)
        object.__setattr__(self, "fps", fps)
        object.__setattr__(self, "workers", workers)
        object.__setattr__(self, "roi", roi)
        object.__setattr__(self, "crop_bottom_ratio", crop_bottom_ratio)
        object.__setattr__(self, "offset_ms", offset_ms)
        object.__setattr__(self, "confidence_threshold", confidence)
        object.__setattr__(self, "same_threshold", same_threshold)
        object.__setattr__(self, "switch_confirm_frames", switch_confirm_frames)
        object.__setattr__(self, "min_line_gap_ms", min_line_gap_ms)
        object.__setattr__(self, "max_blank_frames_inside_line", max_blank_frames)

    @property
    def default_output_path(self) -> Path:
        """Return the explicit output or the CLI's sibling ``.lrc`` default."""

        assert isinstance(self.video_path, Path)
        if isinstance(self.output_path, Path):
            return self.output_path
        return self.video_path.with_suffix(".lrc")

    def to_cli_args(
        self,
        *,
        preview: bool = False,
        event_stream: bool = False,
        job_id: str | None = None,
        cancel_file: PathValue | None = None,
    ) -> list[str]:
        """Build an argument array; no quoting or shell interpolation is used."""

        args = [str(self.video_path)]
        if self.output_path is not None:
            args.extend(("--output", str(self.output_path)))
        args.extend(("--fps", _format_number(self.fps)))
        args.extend(("--workers", str(self.workers)))
        if self.workdir is not None:
            args.extend(("--workdir", str(self.workdir)))
        if self.roi is not None:
            assert isinstance(self.roi, ROI)
            args.extend(("--roi", self.roi.to_cli_value()))
        elif self.crop_bottom_ratio is not None:
            args.extend(("--crop-bottom-ratio", _format_number(self.crop_bottom_ratio)))
        args.extend(("--style", self.style))
        args.extend(("--active-row", self.active_row))
        args.extend(("--offset-ms", str(self.offset_ms)))
        args.extend(("--confidence-threshold", _format_number(self.confidence_threshold)))
        args.extend(("--same-threshold", _format_number(self.same_threshold)))
        args.extend(("--switch-confirm-frames", str(self.switch_confirm_frames)))
        args.extend(("--min-line-gap-ms", str(self.min_line_gap_ms)))
        args.extend(
            ("--max-blank-frames-inside-line", str(self.max_blank_frames_inside_line))
        )

        if self.keep_workdir:
            args.append("--keep-workdir")
        if self.resume:
            args.append("--resume")
        if self.force:
            args.append("--force")
        if self.strict:
            args.append("--strict")
        if preview:
            args.append("--preview-roi")
        if event_stream:
            args.append("--event-stream")
        if job_id is not None:
            job_id = str(job_id)
            if not job_id or "\x00" in job_id:
                raise ValueError("job_id must be a non-empty string without null bytes")
            args.extend(("--job-id", job_id))
        if cancel_file is not None:
            checked_cancel_file = _path(cancel_file, "cancel_file", required=True)
            args.extend(("--cancel-file", str(checked_cancel_file)))
        return args
