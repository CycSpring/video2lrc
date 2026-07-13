from __future__ import annotations

import json

import pytest

from detector import (
    detect_line_switches,
    detect_style,
    same_line,
    select_medoid,
)


def frame(time: float, text: str, confidence: float = 0.95, box=None) -> dict:
    return {
        "frame": f"{round(time * 100):06d}.png",
        "time": time,
        "text": text,
        "confidence": confidence,
        "box": box,
    }


def segmented_frame(time: float, text: str, parts: list[str]) -> dict:
    result = frame(time, text)
    result["selected_line"] = {
        "items": [{"text": part} for part in parts],
    }
    return result


def test_same_line_uses_length_aware_rules() -> None:
    assert same_line("你的美又总在两个时代", "你的美又总在两个时伐")
    assert not same_line("我爱你", "我想你")

    box = [[0, 10], [100, 10], [100, 30], [0, 30]]
    assert same_line(
        "我爱你",
        "我碍你",
        time_gap=0.25,
        box_left=box,
        box_right=box,
        confidence_left=0.93,
        confidence_right=0.62,
    )
    assert not same_line(
        "我爱你",
        "我碍你",
        time_gap=0.25,
        box_left=box,
        box_right=box,
        confidence_left=0.93,
        confidence_right=0.92,
    )
    assert same_line("你", "你好", style="typewriter")
    assert not same_line("你好", "你", style="typewriter")


def test_detect_style_distinguishes_static_and_typewriter_sequences() -> None:
    line_frames = [
        frame(0.0, "第一句歌词"),
        frame(0.25, "第一句歌词"),
        frame(0.5, "第一句歌词"),
        frame(0.75, "第二句歌词"),
        frame(1.0, "第二句歌词"),
    ]
    typewriter_frames = [
        frame(0.0, "你"),
        frame(0.25, "你好"),
        frame(0.5, "你好世"),
        frame(0.75, "你好世界"),
    ]

    assert detect_style(line_frames) == "line"
    assert detect_style(typewriter_frames) == "typewriter"
    assert detect_style([frame(0.0, "只有一帧")]) == "uncertain"


def test_line_state_machine_handles_jitter_blanks_switch_and_flush() -> None:
    frames = [
        frame(0.0, "你的美又总在两个时代"),
        frame(0.25, "你的美又总在两个时代"),
        frame(0.50, "无关水印"),
        frame(0.75, "你的美又总在两个时代"),
        frame(1.00, ""),
        frame(1.25, ""),
        frame(1.50, "你的美又总在两个时代"),
        frame(2.00, "两个不同围旋"),
        frame(2.25, "两个不同围旋"),
    ]

    result = detect_line_switches(frames)

    assert [line["text"] for line in result["lines"]] == [
        "你的美又总在两个时代",
        "两个不同围旋",
    ]
    assert result["lines"][0]["start_time_raw"] == 0.0
    assert result["lines"][0]["end_time"] == 1.5
    assert result["lines"][0]["support_frames"] == 4
    assert result["lines"][1]["start_time_raw"] == 2.0
    assert result["lines"][1]["support_frames"] == 2
    assert result["rejected_candidates"][0]["reason"] == "ocr_jitter"
    assert result["rejected_candidates"][0]["text"] == "无关水印"
    assert result["blank_intervals"][0]["frame_count"] == 2
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_unconfirmed_last_candidate_is_rejected_without_losing_current_line() -> None:
    result = detect_line_switches(
        [
            frame(0.0, "稳定歌词"),
            frame(0.25, "稳定歌词"),
            frame(0.50, "极短字幕"),
        ]
    )

    assert [line["text"] for line in result["lines"]] == ["稳定歌词"]
    assert result["rejected_candidates"][-1]["reason"] == "unconfirmed_eof"
    assert result["rejected_candidates"][-1]["text"] == "极短字幕"


def test_single_high_confidence_frame_is_traceable_as_rejected() -> None:
    result = detect_line_switches([frame(4.0, "啊")])

    assert result["lines"] == []
    assert result["rejected_candidates"][0]["support_frames"] == 1
    assert result["rejected_candidates"][0]["text"] == "啊"


def test_long_blank_breaks_continuous_interval_but_repeated_chorus_is_kept() -> None:
    result = detect_line_switches(
        [
            frame(0.0, "重复副歌"),
            frame(0.25, "重复副歌"),
            frame(0.50, ""),
            frame(0.75, ""),
            frame(1.00, ""),
            frame(4.00, "重复副歌"),
            frame(4.25, "重复副歌"),
        ]
    )

    assert [line["text"] for line in result["lines"]] == ["重复副歌", "重复副歌"]
    assert [line["start_time_raw"] for line in result["lines"]] == [0.0, 4.0]


def test_low_confidence_frames_are_logged_and_do_not_switch_lines() -> None:
    result = detect_line_switches(
        [
            frame(0.0, "主歌词"),
            frame(0.25, "主歌词"),
            frame(0.50, "错误文字", confidence=0.2),
            frame(0.75, "主歌词"),
        ]
    )

    assert len(result["lines"]) == 1
    assert result["lines"][0]["support_frames"] == 3
    assert result["low_confidence_frames"][0]["reason"] == "low_confidence"


def test_empty_low_confidence_status_is_not_counted_as_a_blank() -> None:
    low_confidence_frame = frame(0.5, "", confidence=0.0)
    low_confidence_frame["status"] = "low_confidence"
    result = detect_line_switches(
        [
            frame(0.0, "主歌词"),
            frame(0.25, "主歌词"),
            low_confidence_frame,
            frame(0.75, "主歌词"),
        ]
    )

    assert len(result["lines"]) == 1
    assert result["blank_intervals"] == []
    assert result["low_confidence_frames"][0]["status"] == "low_confidence"


def test_short_text_context_reads_the_ocr_selected_line_box() -> None:
    box = [[0, 10], [100, 10], [100, 30], [0, 30]]
    left = frame(0.0, "我爱你", confidence=0.92)
    right = frame(0.25, "我碍你", confidence=0.60)
    left["selected_line"] = {"box": box}
    right["selected_line"] = {"box": box}

    assert same_line(left, right)


def test_typewriter_state_machine_uses_first_fragment_time_and_full_text() -> None:
    result = detect_line_switches(
        [
            frame(0.0, "你"),
            frame(0.25, "你好"),
            frame(0.50, "你好世"),
            frame(0.75, "你好世界"),
            frame(1.00, "你好世界"),
        ],
        style="typewriter",
    )

    assert len(result["lines"]) == 1
    assert result["lines"][0]["start_time_raw"] == 0.0
    assert result["lines"][0]["text"] == "你好世界"
    assert result["lines"][0]["support_frames"] == 5


def test_typewriter_shorter_prefix_starts_a_new_line_from_its_first_frame() -> None:
    result = detect_line_switches(
        [
            frame(0.0, "我爱你"),
            frame(0.25, "我爱你"),
            frame(1.0, "我"),
            frame(1.25, "我们"),
            frame(1.50, "我们走"),
        ],
        style="typewriter",
    )

    assert [item["text"] for item in result["lines"]] == ["我爱你", "我们走"]
    assert result["lines"][1]["start_time_raw"] == 1.0


def test_short_gap_is_qa_only_and_does_not_merge_lines() -> None:
    result = detect_line_switches(
        [
            frame(0.0, "第一行"),
            frame(0.2, "第一行"),
            frame(0.5, "第二行"),
            frame(0.7, "第二行"),
        ],
        min_line_gap_ms=800,
    )

    assert len(result["lines"]) == 2
    assert "short_gap" in result["lines"][1]["qa_flags"]


def test_stable_similar_long_text_is_preserved_as_a_new_line() -> None:
    first = "你的美又总在两个时代"
    second = "你的美又总在两个时伐"
    assert same_line(first, second)

    result = detect_line_switches(
        [
            frame(0.0, first),
            frame(0.25, first),
            frame(1.0, second),
            frame(1.25, second),
        ]
    )

    assert [item["text"] for item in result["lines"]] == [first, second]
    assert "similar_to_previous" in result["lines"][1]["qa_flags"]


def test_line_mode_does_not_split_two_frame_near_exact_ocr_variant() -> None:
    stable = "你的美又总在两个时代两个不同围旋两种不同期待"
    ocr_variant = "你的美又总在两个时代两个不同围旋两种不同期侍"
    assert same_line(stable, ocr_variant)

    result = detect_line_switches(
        [
            frame(0.0, stable),
            frame(0.25, stable),
            frame(0.50, stable),
            frame(0.75, ocr_variant),
            frame(1.00, ocr_variant),
        ]
    )

    assert [item["text"] for item in result["lines"]] == [stable]
    assert result["lines"][0]["support_frames"] == 3
    assert result["rejected_candidates"][0]["reason"] == "unconfirmed_eof"


def test_line_mode_promotes_persistent_near_exact_new_line() -> None:
    first = "你的美又总在两个时代两个不同围旋两种不同期待"
    second = "你的美又总在两个时代两个不同围旋两种不同期侍"

    result = detect_line_switches(
        [
            frame(0.0, first),
            frame(0.25, first),
            frame(2.0, second),
            frame(2.25, second),
            frame(2.50, second),
        ]
    )

    assert [item["text"] for item in result["lines"]] == [first, second]
    assert "similar_to_previous" in result["lines"][1]["qa_flags"]


def test_line_mode_keeps_multi_edit_similar_neighbor_as_a_new_line() -> None:
    first = "你的美又总在两个时代两个不同围旋两种不同期待"
    second = "你的美又总在两个时代两个不同围旋两种不同愿望"
    assert same_line(first, second)

    result = detect_line_switches(
        [
            frame(0.0, first),
            frame(0.25, first),
            frame(0.50, second),
            frame(0.75, second),
        ]
    )

    assert [item["text"] for item in result["lines"]] == [first, second]
    assert "similar_to_previous" in result["lines"][1]["qa_flags"]


def test_stable_aba_sequence_preserves_confirmed_lines_with_qa() -> None:
    first = "你的美又总在两个时代"
    variant = "你的美又总在两个时伐"

    result = detect_line_switches(
        [
            frame(0.0, first),
            frame(0.25, first),
            frame(0.50, variant),
            frame(0.75, variant),
            frame(1.00, first),
            frame(1.25, first),
        ]
    )

    assert [item["text"] for item in result["lines"]] == [first, variant, first]
    assert [item["support_frames"] for item in result["lines"]] == [2, 2, 2]
    assert "similar_to_previous" in result["lines"][1]["qa_flags"]
    assert "similar_to_previous" in result["lines"][2]["qa_flags"]


def test_contiguous_stable_similar_new_line_is_not_merged_without_recovery() -> None:
    first = "你的美又总在两个时代"
    second = "你的美又总在两个时伐"

    result = detect_line_switches(
        [
            frame(0.0, first),
            frame(0.25, first),
            frame(0.50, second),
            frame(0.75, second),
        ]
    )

    assert [item["text"] for item in result["lines"]] == [first, second]
    assert "similar_to_previous" in result["lines"][1]["qa_flags"]


def test_medoid_uses_total_edit_distance_then_confidence() -> None:
    candidates = [
        {"text": "你的美", "confidence": 0.80},
        {"text": "你的羌", "confidence": 0.99},
        {"text": "你的美啊", "confidence": 0.95},
    ]
    assert select_medoid(candidates) == "你的美"

    tied = [
        {"text": "甲乙", "confidence": 0.60},
        {"text": "甲丙", "confidence": 0.95},
    ]
    assert select_medoid(tied) == "甲丙"


def test_visual_fragment_boundaries_are_voted_into_display_text() -> None:
    text = "你的美又总在两个时代两个不同围旋"
    result = detect_line_switches(
        [
            segmented_frame(0.0, text, ["你的美", "又总在两个时代两个不同围旋"]),
            segmented_frame(0.25, text, ["你的美又总在两个时代", "两个不同围旋"]),
            segmented_frame(0.50, text, ["你的美", "又总在两个时代", "两个不同围旋"]),
            segmented_frame(0.75, text, [text]),
        ]
    )

    line = result["lines"][0]
    assert line["text"] == "你的美 又总在两个时代 两个不同围旋"
    assert line["visual_spaces"] == [
        {"position": 3, "votes": 2, "compatible_frames": 4, "ratio": 0.5},
        {"position": 10, "votes": 2, "compatible_frames": 4, "ratio": 0.5},
    ]


def test_single_frame_fragment_boundary_does_not_add_a_space() -> None:
    text = "这是一句完整歌词"
    result = detect_line_switches(
        [
            segmented_frame(0.0, text, ["这是一句", "完整歌词"]),
            segmented_frame(0.25, text, [text]),
            segmented_frame(0.50, text, [text]),
            segmented_frame(0.75, text, [text]),
        ]
    )

    assert result["lines"][0]["text"] == text
    assert "visual_spaces" not in result["lines"][0]


def test_repeated_explicit_ocr_space_is_preserved_for_display() -> None:
    result = detect_line_switches(
        [
            frame(0.0, "OH 山水 泼墨的感觉"),
            frame(0.25, "OH 山水 泼墨的感觉"),
            frame(0.50, "OH山水泼墨的感觉"),
        ]
    )

    assert result["lines"][0]["text"] == "OH 山水 泼墨的感觉"


def test_single_frame_and_typewriter_keep_observed_english_spaces() -> None:
    single = detect_line_switches(
        [frame(0.0, "I love you")],
        switch_confirm_frames=1,
    )
    typewriter = detect_line_switches(
        [
            frame(0.0, "I"),
            frame(0.25, "I love"),
            frame(0.50, "I love you"),
        ],
        style="typewriter",
    )

    assert single["lines"][0]["text"] == "I love you"
    assert typewriter["lines"][0]["text"] == "I love you"


def test_single_frame_keeps_spaces_around_punctuation_only_fragment() -> None:
    result = detect_line_switches(
        [frame(0.0, "I - love you")],
        switch_confirm_frames=1,
    )

    assert result["lines"][0]["text"] == "I - love you"
