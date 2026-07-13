"""Dependency-free subtitle style and line transition detection."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Any

from config import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE,
    DEFAULT_MIN_LINE_GAP_MS,
    DEFAULT_SAME_THRESHOLD,
    DEFAULT_STYLE,
    DEFAULT_SWITCH_CONFIRM_FRAMES,
    STYLE_CHOICES,
)
from utils import levenshtein_distance, normalize_match_text, text_similarity, text_variants


Frame = Mapping[str, Any]
Candidate = dict[str, Any]

_MIN_VISUAL_SPACE_VOTES = 2
_MIN_VISUAL_SPACE_RATIO = 0.30
_LINE_OCR_VARIANT_MIN_SIMILARITY = 95.0
_LINE_OCR_VARIANT_EXTRA_CONFIRM_FRAMES = 1


def _finite_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _confidence(value: object, default: float) -> float:
    number = _finite_float(value, default)
    if 1.0 < number <= 100.0:
        number /= 100.0
    return min(1.0, max(0.0, number))


def _space_boundaries(text: object, match_text: str) -> set[int]:
    """Return compact-text positions where OCR preserved an explicit space."""

    display_text = text_variants(text)["display_text"]
    parts = display_text.split(" ")
    if len(parts) < 2:
        return set()
    compact_parts = [normalize_match_text(part) for part in parts]
    if any(not part for part in compact_parts) or "".join(compact_parts) != match_text:
        return set()

    boundaries: set[int] = set()
    position = 0
    for part in compact_parts[:-1]:
        position += len(part)
        if 0 < position < len(match_text):
            boundaries.add(position)
    return boundaries


def _fragment_boundaries(frame: Frame, match_text: str) -> set[int]:
    """Return positions where RapidOCR split one visual row into text boxes."""

    selected_line = frame.get("selected_line")
    if not isinstance(selected_line, Mapping):
        return set()
    items = selected_line.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return set()

    parts: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            return set()
        part = normalize_match_text(item.get("text", ""))
        if part:
            parts.append(part)
    if len(parts) < 2 or "".join(parts) != match_text:
        return set()

    boundaries: set[int] = set()
    position = 0
    for part in parts[:-1]:
        position += len(part)
        if 2 <= position <= len(match_text) - 2:
            boundaries.add(position)
    return boundaries


def _candidate_display_boundaries(frame: Frame, display_text: str, match_text: str) -> list[int]:
    boundaries = _space_boundaries(display_text, match_text)
    boundaries.update(_fragment_boundaries(frame, match_text))
    return sorted(boundaries)


def _candidate_from_frame(frame: Frame, index: int) -> Candidate:
    text = frame.get("text", frame.get("display_text", frame.get("raw_text", "")))
    variants = text_variants(text)
    display_text = variants["display_text"]
    time_value = frame.get("time", frame.get("timestamp", frame.get("time_seconds", index)))
    selected_line = frame.get("selected_line")
    selected_box = selected_line.get("box") if isinstance(selected_line, Mapping) else None
    box = frame.get("box")
    if box is None:
        box = frame.get("selected_box")
    if box is None:
        box = selected_box
    candidate: Candidate = {
        "frame": str(frame.get("frame", frame.get("frame_name", index))),
        "time": _finite_float(time_value, float(index)),
        **variants,
        "confidence": _confidence(frame.get("confidence"), 1.0 if display_text else 0.0),
        "box": box,
        "status": str(frame.get("status", "")),
        "_source_index": index,
    }
    candidate["display_boundaries"] = _candidate_display_boundaries(
        frame,
        display_text,
        candidate["match_text"],
    )
    return candidate


def _candidate_from_value(value: object) -> Candidate:
    if isinstance(value, Mapping):
        candidate = _candidate_from_frame(value, 0)
        if "match_text" in value:
            candidate["match_text"] = normalize_match_text(value["match_text"])
        return candidate
    return _candidate_from_frame({"text": value}, 0)


def _box_bounds(box: object) -> tuple[float, float, float, float] | None:
    if not isinstance(box, Sequence) or isinstance(box, (str, bytes)):
        return None
    values = list(box)
    if len(values) == 4 and all(isinstance(item, (int, float)) for item in values):
        x1, y1, x2, y2 = (float(item) for item in values)
        if x2 < x1 or y2 < y1:
            return None
        return x1, y1, x2, y2

    points: list[tuple[float, float]] = []
    for point in values:
        if (
            isinstance(point, Sequence)
            and not isinstance(point, (str, bytes))
            and len(point) >= 2
            and isinstance(point[0], (int, float))
            and isinstance(point[1], (int, float))
        ):
            points.append((float(point[0]), float(point[1])))
    if not points:
        return None
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _same_visual_row(left_box: object, right_box: object) -> bool:
    left = _box_bounds(left_box)
    right = _box_bounds(right_box)
    if left is None or right is None:
        return False
    left_height = max(left[3] - left[1], 1.0)
    right_height = max(right[3] - right[1], 1.0)
    overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    vertical_overlap = overlap / min(left_height, right_height)
    left_center = (left[1] + left[3]) / 2.0
    right_center = (right[1] + right[3]) / 2.0
    center_delta = abs(left_center - right_center) / max(left_height, right_height)
    return vertical_overlap >= 0.60 and center_delta <= 0.40


def same_line(
    left: object,
    right: object,
    *,
    style: str = DEFAULT_STYLE,
    threshold: float = DEFAULT_SAME_THRESHOLD,
    time_gap: float | None = None,
    box_left: object = None,
    box_right: object = None,
    confidence_left: float | None = None,
    confidence_right: float | None = None,
) -> bool:
    """Decide whether two OCR observations can belong to one subtitle line.

    Short text is deliberately conservative: a one-character difference only
    merges when timing, row position, and confidence jointly indicate OCR noise.
    """

    if style not in STYLE_CHOICES:
        raise ValueError(f"unsupported subtitle style: {style}")
    if not 0 <= float(threshold) <= 100:
        raise ValueError("threshold must be between 0 and 100")

    left_candidate = _candidate_from_value(left)
    right_candidate = _candidate_from_value(right)
    left_text = left_candidate["match_text"]
    right_text = right_candidate["match_text"]
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True

    if style == "typewriter" and right_text.startswith(left_text):
        return True

    if style == "auto":
        style = "line"

    if time_gap is None:
        time_gap = abs(right_candidate["time"] - left_candidate["time"])
    if box_left is None:
        box_left = left_candidate.get("box")
    if box_right is None:
        box_right = right_candidate.get("box")
    if confidence_left is None:
        confidence_left = left_candidate.get("confidence")
    if confidence_right is None:
        confidence_right = right_candidate.get("confidence")

    longest = max(len(left_text), len(right_text))
    distance = levenshtein_distance(left_text, right_text)
    similarity = 100.0 * (1.0 - distance / longest)

    if longest < 5:
        if longest < 3 or distance != 1:
            return False
        if time_gap is None or time_gap > 0.50:
            return False
        if not _same_visual_row(box_left, box_right):
            return False
        if confidence_left is None or confidence_right is None:
            return False
        return min(float(confidence_left), float(confidence_right)) < 0.80

    length_ratio = min(len(left_text), len(right_text)) / longest
    return length_ratio >= 0.70 and similarity >= float(threshold)


def detect_style_details(
    frames: Sequence[Frame],
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    same_threshold: float = DEFAULT_SAME_THRESHOLD,
) -> dict[str, Any]:
    """Return a JSON-compatible explanation of the style hint."""

    candidates = [
        _candidate_from_frame(frame, index)
        for index, frame in enumerate(frames)
    ]
    candidates = [
        candidate
        for candidate in candidates
        if candidate["match_text"] and candidate["confidence"] >= confidence_threshold
    ]
    candidates.sort(key=lambda item: (item["time"], item["_source_index"]))

    exact_pairs = 0
    similar_pairs = 0
    prefix_extensions = 0
    changed_pairs = 0
    for left, right in zip(candidates, candidates[1:]):
        left_text = left["match_text"]
        right_text = right["match_text"]
        if left_text == right_text:
            exact_pairs += 1
        elif right_text.startswith(left_text) and len(right_text) > len(left_text):
            prefix_extensions += 1
        elif text_similarity(left_text, right_text, normalize=False) >= same_threshold:
            similar_pairs += 1
        else:
            changed_pairs += 1

    pair_count = max(0, len(candidates) - 1)
    stable_pairs = exact_pairs + similar_pairs
    if (
        pair_count >= 2
        and prefix_extensions >= 2
        and prefix_extensions >= changed_pairs
        and prefix_extensions / pair_count >= 0.25
    ):
        hint = "typewriter"
    elif pair_count >= 1 and stable_pairs / pair_count >= 0.40:
        hint = "line"
    else:
        hint = "uncertain"

    return {
        "style_hint": hint,
        "non_empty_frames": len(candidates),
        "pair_count": pair_count,
        "exact_pairs": exact_pairs,
        "similar_pairs": similar_pairs,
        "prefix_extensions": prefix_extensions,
        "changed_pairs": changed_pairs,
    }


def detect_style(
    frames: Sequence[Frame],
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    same_threshold: float = DEFAULT_SAME_THRESHOLD,
) -> str:
    """Return ``line``, ``typewriter``, or ``uncertain`` without rerunning OCR."""

    return detect_style_details(
        frames,
        confidence_threshold=confidence_threshold,
        same_threshold=same_threshold,
    )["style_hint"]


def _candidate_text(value: object) -> tuple[str, str, float]:
    candidate = _candidate_from_value(value)
    return candidate["display_text"], candidate["match_text"], candidate["confidence"]


def select_medoid(candidates: Sequence[object]) -> str:
    """Select the observed text with minimum total edit distance."""

    observations = [_candidate_text(candidate) for candidate in candidates]
    observations = [item for item in observations if item[1]]
    if not observations:
        return ""

    variants: dict[str, dict[str, Any]] = {}
    for index, (display, match, confidence) in enumerate(observations):
        entry = variants.setdefault(
            display,
            {"match": match, "confidences": [], "first_index": index},
        )
        entry["confidences"].append(confidence)

    def rank(item: tuple[str, dict[str, Any]]) -> tuple[float, float, int, int]:
        display, entry = item
        total_distance = sum(
            levenshtein_distance(entry["match"], other_match)
            for _, other_match, _ in observations
        )
        average_confidence = fmean(entry["confidences"])
        return (
            float(total_distance),
            -average_confidence,
            -len(entry["match"]),
            int(entry["first_index"]),
        )

    return min(variants.items(), key=rank)[0]


def _public_candidate(candidate: Candidate) -> dict[str, Any]:
    result = {
        "frame": candidate["frame"],
        "time": candidate["time"],
        "raw_text": candidate["raw_text"],
        "display_text": candidate["display_text"],
        "match_text": candidate["match_text"],
        "confidence": candidate["confidence"],
    }
    if candidate.get("display_boundaries"):
        result["display_boundaries"] = list(candidate["display_boundaries"])
    if candidate.get("status"):
        result["status"] = candidate["status"]
    if candidate.get("box") is not None:
        result["box"] = candidate["box"]
    return result


def _representative_candidate(cluster: Sequence[Candidate], style: str) -> Candidate:
    if style == "typewriter":
        tail = cluster[-max(3, min(5, len(cluster))):]
        return max(
            tail,
            key=lambda item: (
                len(item["match_text"]),
                item["confidence"],
                item["time"],
            ),
        )
    medoid_text = select_medoid(cluster)
    matching = [item for item in cluster if item["display_text"] == medoid_text]
    return max(matching or list(cluster), key=lambda item: item["confidence"])


def _insert_display_spaces(display_text: str, boundaries: Sequence[int]) -> str:
    boundary_set = set(boundaries)
    result: list[str] = []
    match_position = 0
    pending_space = False

    for character in display_text:
        if character.isspace():
            if result and result[-1] != " ":
                result.append(" ")
            pending_space = False
            continue
        contribution = normalize_match_text(character)
        if contribution and pending_space and result and result[-1] != " ":
            result.append(" ")
        if contribution:
            pending_space = False
        result.append(character)
        match_position += len(contribution)
        if match_position in boundary_set:
            pending_space = True
    return "".join(result).strip()


def _display_text_from_cluster(
    cluster: Sequence[Candidate],
    representative: Candidate,
) -> tuple[str, list[dict[str, float | int]]]:
    compatible = [
        item
        for item in cluster
        if item["match_text"] == representative["match_text"]
    ]
    votes: Counter[int] = Counter()
    for item in compatible:
        votes.update(set(item.get("display_boundaries", [])))

    minimum_votes = max(
        _MIN_VISUAL_SPACE_VOTES,
        math.ceil(len(compatible) * _MIN_VISUAL_SPACE_RATIO),
    )
    accepted = sorted(
        position
        for position, vote_count in votes.items()
        if vote_count >= minimum_votes
        and 0 < position < len(representative["match_text"])
    )
    details = (
        [
            {
                "position": position,
                "votes": votes[position],
                "compatible_frames": len(compatible),
                "ratio": round(votes[position] / len(compatible), 6),
            }
            for position in accepted
        ]
        if compatible
        else []
    )
    return _insert_display_spaces(representative["display_text"], accepted), details


def _cluster_matches(
    cluster: Sequence[Candidate],
    candidate: Candidate,
    *,
    style: str,
    threshold: float,
) -> bool:
    representative = _representative_candidate(cluster, style)
    return same_line(
        representative,
        candidate,
        style=style,
        threshold=threshold,
    )


def _is_near_exact_line_variant(
    cluster: Sequence[Candidate],
    candidate: Candidate,
    *,
    threshold: float,
) -> bool:
    """Accept only a very narrow class of repeated whole-line OCR noise.

    ``same_line`` is intentionally broad enough to flag similar neighboring
    lyrics for QA. This narrower check only selects transitions that need one
    extra confirmation frame; it never merges the candidate by itself.
    """

    representative = _representative_candidate(cluster, "line")
    left_text = representative["match_text"]
    right_text = candidate["match_text"]
    if not left_text or len(left_text) != len(right_text):
        return False
    if levenshtein_distance(left_text, right_text) != 1:
        return False
    strict_threshold = max(float(threshold), _LINE_OCR_VARIANT_MIN_SIMILARITY)
    return text_similarity(left_text, right_text, normalize=False) >= strict_threshold


def _text_was_observed(cluster: Sequence[Candidate], candidate: Candidate) -> bool:
    return any(item["match_text"] == candidate["match_text"] for item in cluster)


def _line_from_cluster(cluster: Sequence[Candidate], style: str) -> dict[str, Any]:
    representative = _representative_candidate(cluster, style)
    display_text, visual_spaces = _display_text_from_cluster(cluster, representative)
    start_time = min(item["time"] for item in cluster)
    end_time = max(item["time"] for item in cluster)
    result = {
        "start_time_raw": start_time,
        "start_time_offset": start_time,
        "end_time": end_time,
        "text": display_text,
        "confidence": round(fmean(item["confidence"] for item in cluster), 6),
        "support_frames": len(cluster),
        "text_candidates": [_public_candidate(item) for item in cluster],
        "qa_flags": [],
    }
    if visual_spaces:
        result["visual_spaces"] = visual_spaces
    return result


def _rejected_from_cluster(
    cluster: Sequence[Candidate],
    reason: str,
    style: str,
) -> dict[str, Any]:
    representative = _representative_candidate(cluster, style)
    display_text, visual_spaces = _display_text_from_cluster(cluster, representative)
    result = {
        "reason": reason,
        "start_time": min(item["time"] for item in cluster),
        "end_time": max(item["time"] for item in cluster),
        "text": display_text,
        "confidence": round(fmean(item["confidence"] for item in cluster), 6),
        "support_frames": len(cluster),
        "text_candidates": [_public_candidate(item) for item in cluster],
    }
    if visual_spaces:
        result["visual_spaces"] = visual_spaces
    return result


def _blank_intervals(candidates: Sequence[Candidate]) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    current: list[Candidate] = []
    for candidate in candidates:
        if not candidate["display_text"] and candidate.get("status") != "low_confidence":
            current.append(candidate)
            continue
        if current:
            intervals.append(
                {
                    "start_time": current[0]["time"],
                    "end_time": current[-1]["time"],
                    "frame_count": len(current),
                    "frames": [item["frame"] for item in current],
                }
            )
            current = []
    if current:
        intervals.append(
            {
                "start_time": current[0]["time"],
                "end_time": current[-1]["time"],
                "frame_count": len(current),
                "frames": [item["frame"] for item in current],
            }
        )
    return intervals


def detect_line_switches(
    frames: Sequence[Frame],
    *,
    style: str = DEFAULT_STYLE,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    same_threshold: float = DEFAULT_SAME_THRESHOLD,
    switch_confirm_frames: int = DEFAULT_SWITCH_CONFIRM_FRAMES,
    min_line_gap_ms: int = DEFAULT_MIN_LINE_GAP_MS,
    max_blank_frames_inside_line: int = DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE,
) -> dict[str, Any]:
    """Cluster sequential OCR frames into line events with diagnostics."""

    if style not in STYLE_CHOICES:
        raise ValueError(f"unsupported subtitle style: {style}")
    if not 0 <= confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be between 0 and 1")
    if not 0 <= same_threshold <= 100:
        raise ValueError("same_threshold must be between 0 and 100")
    if switch_confirm_frames < 1:
        raise ValueError("switch_confirm_frames must be at least 1")
    if min_line_gap_ms < 0:
        raise ValueError("min_line_gap_ms cannot be negative")
    if max_blank_frames_inside_line < 0:
        raise ValueError("max_blank_frames_inside_line cannot be negative")

    style_details = detect_style_details(
        frames,
        confidence_threshold=confidence_threshold,
        same_threshold=same_threshold,
    )
    effective_style = style
    if style == "auto":
        effective_style = style_details["style_hint"]
        if effective_style == "uncertain":
            effective_style = "line"

    candidates = [
        _candidate_from_frame(frame, index)
        for index, frame in enumerate(frames)
    ]
    candidates.sort(key=lambda item: (item["time"], item["_source_index"]))

    lines: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []
    current: list[Candidate] = []
    pending: list[Candidate] = []
    blank_streak = 0

    def close_current() -> None:
        nonlocal current
        if current:
            lines.append(_line_from_cluster(current, effective_style))
            current = []

    def reject_pending(reason: str) -> None:
        nonlocal pending
        if pending:
            rejected.append(_rejected_from_cluster(pending, reason, effective_style))
            pending = []

    def promote_pending() -> None:
        nonlocal current, pending
        required_frames = switch_confirm_frames
        if (
            current
            and pending
            and effective_style == "line"
            and _is_near_exact_line_variant(
                current,
                pending[0],
                threshold=same_threshold,
            )
        ):
            required_frames += _LINE_OCR_VARIANT_EXTRA_CONFIRM_FRAMES
        if len(pending) >= required_frames:
            close_current()
            current = pending
            pending = []

    for candidate in candidates:
        if candidate.get("status") == "low_confidence":
            item = _public_candidate(candidate)
            item["reason"] = "low_confidence"
            low_confidence.append(item)
            continue
        if not candidate["display_text"]:
            blank_streak += 1
            if blank_streak > max_blank_frames_inside_line:
                close_current()
                reject_pending("blank_timeout")
            continue

        if candidate["confidence"] < confidence_threshold:
            item = _public_candidate(candidate)
            item["reason"] = "low_confidence"
            low_confidence.append(item)
            continue
        blank_streak = 0

        if not current:
            if not pending:
                pending = [candidate]
            elif _cluster_matches(
                pending,
                candidate,
                style=effective_style,
                threshold=same_threshold,
            ):
                pending.append(candidate)
            else:
                reject_pending("unstable_pending")
                pending = [candidate]
            promote_pending()
            continue

        if (
            _text_was_observed(current, candidate)
            or (
                effective_style == "typewriter"
                and _cluster_matches(
                    current,
                    candidate,
                    style=effective_style,
                    threshold=same_threshold,
                )
            )
        ):
            reject_pending("ocr_jitter")
            current.append(candidate)
            continue

        if not pending:
            pending = [candidate]
        elif _cluster_matches(
            pending,
            candidate,
            style=effective_style,
            threshold=same_threshold,
        ):
            pending.append(candidate)
        else:
            reject_pending("unstable_pending")
            pending = [candidate]
        promote_pending()

    if pending:
        if not current and len(pending) >= switch_confirm_frames:
            current = pending
            pending = []
        else:
            reject_pending("unconfirmed_eof")
    close_current()

    lines.sort(key=lambda item: item["start_time_raw"])
    for previous, line in zip(lines, lines[1:]):
        gap_ms = (line["start_time_raw"] - previous["start_time_raw"]) * 1_000.0
        if gap_ms < min_line_gap_ms:
            line["qa_flags"].append("short_gap")
        if (
            normalize_match_text(previous.get("text", ""))
            != normalize_match_text(line.get("text", ""))
            and same_line(
                previous.get("text", ""),
                line.get("text", ""),
                threshold=same_threshold,
            )
        ):
            line["qa_flags"].append("similar_to_previous")

    return {
        "schema_version": 1,
        "style": effective_style,
        "style_hint": style_details["style_hint"],
        "style_details": style_details,
        "lines": lines,
        "blank_intervals": _blank_intervals(candidates),
        "low_confidence_frames": low_confidence,
        "rejected_candidates": rejected,
        "parameters": {
            "confidence_threshold": confidence_threshold,
            "same_threshold": same_threshold,
            "switch_confirm_frames": switch_confirm_frames,
            "min_line_gap_ms": min_line_gap_ms,
            "max_blank_frames_inside_line": max_blank_frames_inside_line,
        },
    }


detect_lines = detect_line_switches


__all__ = [
    "detect_line_switches",
    "detect_lines",
    "detect_style",
    "detect_style_details",
    "same_line",
    "select_medoid",
]
