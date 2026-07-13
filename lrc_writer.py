"""LRC rendering and review CSV output."""

from __future__ import annotations

import copy
import csv
import io
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from config import DEFAULT_OFFSET_MS
from utils import atomic_write_text, format_lrc_timestamp, seconds_to_centiseconds


class TimestampOrderError(ValueError):
    """Raised when strict output contains equal or decreasing timestamps."""


def _extract_lines(lines_or_result: object) -> list[Mapping[str, Any]]:
    if isinstance(lines_or_result, Mapping):
        value = lines_or_result.get("lines", [])
    else:
        value = lines_or_result
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("lines must be a sequence or a detector result mapping")
    lines = list(value)
    if not all(isinstance(line, Mapping) for line in lines):
        raise TypeError("every line must be a mapping")
    return lines


def _number(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _flags(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence):
        return list(dict.fromkeys(str(item) for item in value if str(item)))
    return [str(value)]


def _append_flag(line: dict[str, Any], flag: str) -> None:
    flags = _flags(line.get("qa_flags"))
    if flag not in flags:
        flags.append(flag)
    line["qa_flags"] = flags


def apply_offset(
    lines_or_result: object,
    offset_ms: int | float = DEFAULT_OFFSET_MS,
) -> list[dict[str, Any]]:
    """Copy lines, apply a global offset, and clamp negative starts to zero."""

    offset = _number(offset_ms, "offset_ms") / 1_000.0
    result: list[dict[str, Any]] = []
    for source in _extract_lines(lines_or_result):
        line = copy.deepcopy(dict(source))
        raw_value = line.get("start_time_raw", line.get("start_time"))
        if raw_value is None:
            raise ValueError("line is missing start_time_raw")
        raw_start = _number(raw_value, "start_time_raw")
        final_start = raw_start + offset
        line["start_time_raw"] = raw_start
        line["qa_flags"] = _flags(line.get("qa_flags"))
        if final_start < 0:
            final_start = 0.0
            _append_flag(line, "negative_offset_clamped")
        line["start_time_offset"] = final_start
        if "end_time" in line:
            line["end_time"] = _number(line["end_time"], "end_time")
        result.append(line)
    return result


def prepare_lrc_lines(
    lines_or_result: object,
    *,
    offset_ms: int | float = DEFAULT_OFFSET_MS,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Apply offset, annotate ordering problems, then sort for output."""

    lines = apply_offset(lines_or_result, offset_ms)
    violations: list[str] = []
    centiseconds = [
        seconds_to_centiseconds(line["start_time_offset"])
        for line in lines
    ]

    for index in range(1, len(lines)):
        if centiseconds[index] == centiseconds[index - 1]:
            _append_flag(lines[index], "duplicate_timestamp")
            violations.append(f"lines {index} and {index + 1} round to the same timestamp")
        elif centiseconds[index] < centiseconds[index - 1]:
            _append_flag(lines[index], "out_of_order_timestamp")
            violations.append(f"line {index + 1} precedes line {index} after rounding")

    indexed = list(enumerate(zip(lines, centiseconds)))
    indexed.sort(key=lambda item: (item[1][1], item[0]))
    sorted_lines = [item[1][0] for item in indexed]
    sorted_centiseconds = [item[1][1] for item in indexed]
    for index in range(1, len(sorted_lines)):
        if sorted_centiseconds[index] == sorted_centiseconds[index - 1]:
            _append_flag(sorted_lines[index], "duplicate_timestamp")
            message = f"multiple lines round to centisecond {sorted_centiseconds[index]}"
            if message not in violations:
                violations.append(message)

    if strict and violations:
        raise TimestampOrderError("; ".join(violations))
    return sorted_lines


def _render_prepared(lines: Sequence[Mapping[str, Any]]) -> str:
    rendered = [
        f"{format_lrc_timestamp(line['start_time_offset'])}{line.get('text', '')}"
        for line in lines
    ]
    return "\n".join(rendered) + ("\n" if rendered else "")


def render_lrc(
    lines_or_result: object,
    *,
    offset_ms: int | float = DEFAULT_OFFSET_MS,
    strict: bool = False,
) -> str:
    """Render pure line-level LRC text with LF newlines."""

    return _render_prepared(
        prepare_lrc_lines(lines_or_result, offset_ms=offset_ms, strict=strict)
    )


def _review_csv_text(lines: Sequence[Mapping[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "line_no",
            "start_raw",
            "start_final",
            "end",
            "text",
            "confidence",
            "support_frames",
            "qa_flags",
        )
    )
    for line_no, line in enumerate(lines, start=1):
        writer.writerow(
            (
                line_no,
                f"{_number(line['start_time_raw'], 'start_time_raw'):.3f}",
                f"{_number(line['start_time_offset'], 'start_time_offset'):.3f}",
                (
                    f"{_number(line['end_time'], 'end_time'):.3f}"
                    if line.get("end_time") is not None
                    else ""
                ),
                line.get("text", ""),
                (
                    f"{_number(line['confidence'], 'confidence'):.4f}"
                    if line.get("confidence") is not None
                    else ""
                ),
                line.get("support_frames", ""),
                ";".join(_flags(line.get("qa_flags"))),
            )
        )
    return stream.getvalue()


def _ensure_available(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"output already exists: {path}")


def write_lrc(
    lines_or_result: object,
    output_path: str | os.PathLike[str] | None,
    *,
    offset_ms: int | float = DEFAULT_OFFSET_MS,
    force: bool = False,
    strict: bool = False,
    dry_run: bool = False,
    preview_lines: int = 10,
    review_path: str | os.PathLike[str] | None = None,
    review_force: bool | None = None,
) -> dict[str, Any]:
    """Write LRC and, when requested, a synchronized review CSV."""

    if preview_lines < 0:
        raise ValueError("preview_lines cannot be negative")
    prepared = prepare_lrc_lines(lines_or_result, offset_ms=offset_ms, strict=strict)
    lrc_text = _render_prepared(prepared)
    preview = lrc_text.splitlines()[:preview_lines]
    if dry_run:
        return {
            "written": False,
            "dry_run": True,
            "output_path": None,
            "review_path": None,
            "would_write_output_path": str(output_path) if output_path is not None else None,
            "would_write_review_path": str(review_path) if review_path is not None else None,
            "line_count": len(prepared),
            "preview": preview,
            "lines": prepared,
        }
    if output_path is None:
        raise ValueError("output_path is required unless dry_run is enabled")

    target = Path(output_path)
    review_target = Path(review_path) if review_path is not None else None
    overwrite_review = force if review_force is None else review_force
    _ensure_available(target, force)
    if review_target is not None:
        _ensure_available(review_target, overwrite_review)

    atomic_write_text(target, lrc_text, encoding="utf-8", overwrite=force)
    if review_target is not None:
        atomic_write_text(
            review_target,
            _review_csv_text(prepared),
            encoding="utf-8-sig",
            overwrite=overwrite_review,
        )
    return {
        "written": True,
        "dry_run": False,
        "output_path": str(target),
        "review_path": str(review_target) if review_target is not None else None,
        "line_count": len(prepared),
        "preview": preview,
        "lines": prepared,
    }


def write_review_csv(
    lines_or_result: object,
    review_path: str | os.PathLike[str],
    *,
    offset_ms: int | float = DEFAULT_OFFSET_MS,
    force: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Write an Excel-friendly UTF-8 BOM review file."""

    prepared = prepare_lrc_lines(lines_or_result, offset_ms=offset_ms, strict=strict)
    target = Path(review_path)
    _ensure_available(target, force)
    atomic_write_text(
        target,
        _review_csv_text(prepared),
        encoding="utf-8-sig",
        overwrite=force,
    )
    return {
        "written": True,
        "review_path": str(target),
        "line_count": len(prepared),
        "lines": prepared,
    }


def write_outputs(
    lines_or_result: object,
    output_path: str | os.PathLike[str],
    *,
    review_path: str | os.PathLike[str] | None = None,
    offset_ms: int | float = DEFAULT_OFFSET_MS,
    force: bool = False,
    strict: bool = False,
    dry_run: bool = False,
    preview_lines: int = 10,
    review_force: bool | None = None,
) -> dict[str, Any]:
    """Write an LRC plus ``review.csv`` beside it by default."""

    target = Path(output_path)
    actual_review_path = (
        Path(review_path) if review_path is not None else target.with_name("review.csv")
    )
    return write_lrc(
        lines_or_result,
        target,
        offset_ms=offset_ms,
        force=force,
        strict=strict,
        dry_run=dry_run,
        preview_lines=preview_lines,
        review_path=actual_review_path,
        review_force=review_force,
    )


write_review_files = write_review_csv


__all__ = [
    "TimestampOrderError",
    "apply_offset",
    "prepare_lrc_lines",
    "render_lrc",
    "write_lrc",
    "write_outputs",
    "write_review_csv",
    "write_review_files",
]
