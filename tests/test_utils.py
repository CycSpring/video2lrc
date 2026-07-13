from __future__ import annotations

import json
from io import BytesIO, TextIOWrapper

import pytest
import utils as utils_module

from utils import (
    atomic_write_json,
    configure_utf8_stdio,
    format_lrc_timestamp,
    normalize_match_text,
    normalize_text,
    seconds_to_centiseconds,
    text_similarity,
    text_variants,
)


def test_configure_utf8_stdio_handles_non_gbk_lyrics(monkeypatch) -> None:
    buffer = BytesIO()
    stream = TextIOWrapper(buffer, encoding="cp936")
    monkeypatch.setattr(utils_module.sys, "stdout", stream)

    configure_utf8_stdio()
    stream.write("歌词\u2764")
    stream.flush()

    assert buffer.getvalue().decode("utf-8") == "歌词\u2764"


def test_text_normalization_keeps_display_and_match_layers_separate() -> None:
    raw = "  Ａ＆Ｂ\u3000don’t，停！  \u266a "

    assert normalize_text(raw) == "A&B don’t,停!"
    assert normalize_match_text(raw) == "a&bdon't停"
    assert text_variants(raw) == {
        "raw_text": raw,
        "display_text": "A&B don’t,停!",
        "match_text": "a&bdon't停",
    }


def test_text_similarity_uses_normalized_edit_distance() -> None:
    assert text_similarity("你的美，又总在两个时代", "你的美 又总在两个时代") == 100.0
    assert text_similarity("你的美又总在两个时代", "你的美又总在两个时伐") == pytest.approx(90.0)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "[00:00.00]"),
        (59.995, "[01:00.00]"),
        (65.004, "[01:05.00]"),
        (65.005, "[01:05.01]"),
        (-1.2, "[00:00.00]"),
        (6_005.67, "[100:05.67]"),
    ],
)
def test_lrc_timestamp_uses_half_up_centisecond_rounding(seconds: float, expected: str) -> None:
    assert format_lrc_timestamp(seconds) == expected


def test_seconds_to_centiseconds_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError):
        seconds_to_centiseconds(float("nan"))


def test_atomic_json_is_utf8_and_does_not_leave_temp_files(tmp_path) -> None:
    output = tmp_path / "nested" / "data.json"
    atomic_write_json(output, {"text": "中文", "value": 2})

    assert json.loads(output.read_text(encoding="utf-8")) == {"text": "中文", "value": 2}
    assert not list(output.parent.glob("*.tmp"))
