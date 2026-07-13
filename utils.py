"""Small dependency-free helpers shared by the pipeline."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


_DECORATIVE_CHARS = frozenset(
    "\u266a\u266b\u266c\u2669\u2605\u2606\u2665\u2661"
    "\u25c6\u25c7\u25cf\u25cb\u25a0\u25a1\u25b2\u25b3"
)
_MATCH_PUNCTUATION_TO_KEEP = frozenset(("&", "'"))
_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'"})


def configure_utf8_stdio() -> None:
    """Use UTF-8 for CLI streams when the host exposes reconfigurable streams."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (OSError, ValueError):
                # Embedded and captured streams may reject reconfiguration.
                continue


def normalize_text(text: object) -> str:
    """Normalize text for display without erasing meaningful lyric symbols."""

    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    cleaned: list[str] = []
    for char in normalized:
        if char in _DECORATIVE_CHARS:
            continue
        category = unicodedata.category(char)
        if char.isspace():
            cleaned.append(" ")
        elif category in {"Cf", "Cc"}:
            continue
        else:
            cleaned.append(char)
    return " ".join("".join(cleaned).split())


def normalize_match_text(text: object) -> str:
    """Build a compact comparison form while preserving ``&`` and apostrophes."""

    display = normalize_text(text).translate(_APOSTROPHES).casefold()
    result: list[str] = []
    for char in display:
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category.startswith("P") and char not in _MATCH_PUNCTUATION_TO_KEEP:
            continue
        result.append(char)
    return "".join(result)


def text_variants(text: object) -> dict[str, str]:
    """Return the raw, display, and matching representations of OCR text."""

    raw_text = "" if text is None else str(text)
    display_text = normalize_text(raw_text)
    return {
        "raw_text": raw_text,
        "display_text": display_text,
        "match_text": normalize_match_text(display_text),
    }


def levenshtein_distance(left: str, right: str) -> int:
    """Return the insertion/deletion/substitution distance between two strings."""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) > len(right):
        left, right = right, left

    previous = list(range(len(left) + 1))
    for row, right_char in enumerate(right, start=1):
        current = [row]
        for column, left_char in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def text_similarity(left: object, right: object, *, normalize: bool = True) -> float:
    """Return normalized edit similarity on a 0-100 scale."""

    left_text = normalize_match_text(left) if normalize else str(left)
    right_text = normalize_match_text(right) if normalize else str(right)
    longest = max(len(left_text), len(right_text))
    if longest == 0:
        return 100.0
    distance = levenshtein_distance(left_text, right_text)
    return max(0.0, 100.0 * (1.0 - distance / longest))


def _decimal_seconds(value: object) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("seconds must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid seconds value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("seconds must be finite")
    return result


def seconds_to_centiseconds(seconds: object, *, clamp_zero: bool = True) -> int:
    """Round seconds to LRC centiseconds using conventional half-up rounding."""

    value = _decimal_seconds(seconds)
    if clamp_zero and value < 0:
        value = Decimal(0)
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_lrc_timestamp(seconds: object) -> str:
    """Format seconds as an LRC ``[mm:ss.xx]`` timestamp."""

    total_centiseconds = seconds_to_centiseconds(seconds)
    minutes, remainder = divmod(total_centiseconds, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"[{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}]"


def atomic_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    overwrite: bool = True,
) -> Path:
    """Atomically publish complete bytes from a temporary file in the same directory."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and target.exists():
        raise FileExistsError(f"output already exists: {target}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if not overwrite and target.exists():
            raise FileExistsError(f"output already exists: {target}")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    overwrite: bool = True,
) -> Path:
    """Encode and atomically write text."""

    return atomic_write_bytes(path, text.encode(encoding), overwrite=overwrite)


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
    *,
    overwrite: bool = True,
    indent: int = 2,
) -> Path:
    """Write JSON with UTF-8, stable newlines, and an atomic replace."""

    payload = json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False) + "\n"
    return atomic_write_text(path, payload, overwrite=overwrite)
