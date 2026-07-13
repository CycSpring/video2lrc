from __future__ import annotations

import csv

import pytest

from lrc_writer import (
    TimestampOrderError,
    apply_offset,
    prepare_lrc_lines,
    render_lrc,
    write_lrc,
)


def line(start: float, text: str, **values) -> dict:
    return {
        "start_time_raw": start,
        "start_time_offset": start,
        "end_time": start + 1,
        "text": text,
        "confidence": 0.9,
        "support_frames": 3,
        "qa_flags": [],
        **values,
    }


def test_negative_offset_is_clamped_without_mutating_input() -> None:
    source = [line(0.2, "开头")]
    shifted = apply_offset(source, -500)

    assert shifted[0]["start_time_offset"] == 0.0
    assert "negative_offset_clamped" in shifted[0]["qa_flags"]
    assert source[0]["start_time_offset"] == 0.2
    assert source[0]["qa_flags"] == []


def test_render_lrc_is_sorted_and_uses_lf() -> None:
    source = [line(2.0, "第二句"), line(1.0, "第一句")]

    assert render_lrc(source) == "[00:01.00]第一句\n[00:02.00]第二句\n"
    prepared = prepare_lrc_lines(source)
    assert "out_of_order_timestamp" in next(
        item for item in prepared if item["text"] == "第一句"
    )["qa_flags"]


def test_strict_order_is_checked_after_centisecond_rounding() -> None:
    source = [line(1.001, "甲"), line(1.004, "乙")]

    with pytest.raises(TimestampOrderError, match="same timestamp"):
        render_lrc(source, strict=True)

    prepared = prepare_lrc_lines(source)
    assert "duplicate_timestamp" in prepared[1]["qa_flags"]


def test_write_lrc_is_utf8_without_bom_and_review_is_utf8_with_bom(tmp_path) -> None:
    output = tmp_path / "歌.lrc"
    review = tmp_path / "review.csv"
    result = write_lrc(
        {"lines": [line(0.125, "中文,歌词")]},
        output,
        review_path=review,
    )

    assert result["written"] is True
    assert output.read_bytes() == "[00:00.13]中文,歌词\n".encode("utf-8")
    assert not output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert review.read_bytes().startswith(b"\xef\xbb\xbf")
    with review.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["text"] == "中文,歌词"
    assert rows[0]["start_raw"] == "0.125"
    assert rows[0]["start_final"] == "0.125"


def test_existing_output_is_refused_unless_force_is_set(tmp_path) -> None:
    output = tmp_path / "output.lrc"
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_lrc([line(1.0, "歌词")], output)
    assert output.read_text(encoding="utf-8") == "keep"

    write_lrc([line(1.0, "歌词")], output, force=True)
    assert output.read_text(encoding="utf-8") == "[00:01.00]歌词\n"


def test_internal_review_can_be_replaced_without_forcing_output(tmp_path) -> None:
    output = tmp_path / "new-output.lrc"
    review = tmp_path / "review.csv"
    review.write_text("old", encoding="utf-8")

    write_lrc(
        [line(1.0, "歌词")],
        output,
        review_path=review,
        review_force=True,
    )

    assert output.read_text(encoding="utf-8") == "[00:01.00]歌词\n"
    assert review.read_text(encoding="utf-8-sig").startswith("line_no,")


def test_dry_run_does_not_require_or_write_a_path() -> None:
    result = write_lrc(
        [line(3.0, "预览")],
        None,
        dry_run=True,
        preview_lines=1,
    )

    assert result["written"] is False
    assert result["preview"] == ["[00:03.00]预览"]
    assert result["output_path"] is None
    assert result["review_path"] is None
