"""Project-wide defaults for the video-to-LRC pipeline."""

from __future__ import annotations

from typing import Any


DEFAULT_FPS = 4
DEFAULT_WORKERS = 4
DEFAULT_CROP_BOTTOM_RATIO = 0.40
DEFAULT_CONFIDENCE_THRESHOLD = 0.50
DEFAULT_RAW_OCR_TEXT_SCORE = 0.10
DEFAULT_SAME_THRESHOLD = 90.0
DEFAULT_SWITCH_CONFIRM_FRAMES = 2
DEFAULT_MIN_LINE_GAP_MS = 800
DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE = 2
DEFAULT_OFFSET_MS = 0
DEFAULT_STYLE = "line"
DEFAULT_ACTIVE_ROW = "auto"

STYLE_CHOICES = ("line", "typewriter", "auto")
ACTIVE_ROW_CHOICES = ("auto", "top", "bottom")

DEFAULT_CONFIG: dict[str, Any] = {
    "fps": DEFAULT_FPS,
    "workers": DEFAULT_WORKERS,
    "crop_bottom_ratio": DEFAULT_CROP_BOTTOM_RATIO,
    "confidence_threshold": DEFAULT_CONFIDENCE_THRESHOLD,
    "raw_ocr_text_score": DEFAULT_RAW_OCR_TEXT_SCORE,
    "same_threshold": DEFAULT_SAME_THRESHOLD,
    "switch_confirm_frames": DEFAULT_SWITCH_CONFIRM_FRAMES,
    "min_line_gap_ms": DEFAULT_MIN_LINE_GAP_MS,
    "max_blank_frames_inside_line": DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE,
    "offset_ms": DEFAULT_OFFSET_MS,
    "style": DEFAULT_STYLE,
    "active_row": DEFAULT_ACTIVE_ROW,
}


def get_default_config() -> dict[str, Any]:
    """Return a mutable, JSON-compatible copy of the default configuration."""

    return DEFAULT_CONFIG.copy()
