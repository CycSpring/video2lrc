"""Evaluate generated LRC lyrics against a reference LRC.

Alignment is based on lyric text only. It is global, monotonic, and one-to-one,
so repeated chorus lines cannot all be assigned to the same reference line.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from utils import atomic_write_text, configure_utf8_stdio

try:  # rapidfuzz is optional so reports still work in a minimal environment.
    from rapidfuzz.distance import Levenshtein as _RapidLevenshtein
except ImportError:  # pragma: no cover - which branch runs depends on the environment.
    _RapidLevenshtein = None


_TIMESTAMP_RE = re.compile(
    r"\[(?P<minutes>\d+):(?P<seconds>[0-5]?\d)"
    r"(?:(?:\.|:)(?P<fraction>\d{1,3}))?\]"
)
_OFFSET_RE = re.compile(r"\[offset\s*:\s*(?P<milliseconds>[+-]?\d+)\s*\]", re.IGNORECASE)
_PRESERVED_MATCH_SYMBOLS = frozenset({"&", "'", "’", "+", "#"})


@dataclass(frozen=True, slots=True)
class LRCLine:
    """A timestamped lyric line parsed from an LRC document."""

    timestamp: float
    text: str
    source_line: int = 0


@dataclass(frozen=True, slots=True)
class LineAlignment:
    """Indexes and text score for a single reference/candidate match."""

    reference_index: int
    candidate_index: int
    similarity: float
    edit_distance: int


@dataclass(slots=True)
class _DPCell:
    score: float
    matches: int
    exact_matches: int
    position_cost: float
    gaps: int
    previous: tuple[int, int] | None
    operation: str | None

    def key(self) -> tuple[float, int, int, float, int]:
        # Position is deliberately only a tie-breaker. Lyrics, not timestamps or
        # track position, determine whether two lines match.
        return (
            round(self.score, 12),
            self.matches,
            self.exact_matches,
            -round(self.position_cost, 12),
            -self.gaps,
        )


def parse_lrc(content: str, *, apply_offset_tag: bool = True) -> list[LRCLine]:
    """Parse timestamped lines from pure or metadata-tagged LRC text.

    Metadata such as ``[ti:]``, ``[ar:]``, and ``[al:]`` is ignored. Multiple
    timestamps on one lyric line are expanded. A standard ``[offset:+/-N]`` tag
    is applied by default because it changes the playback time of every line.
    """

    if not isinstance(content, str):
        raise TypeError("content must be a string")

    offset_seconds = 0.0
    if apply_offset_tag:
        offset_matches = list(_OFFSET_RE.finditer(content))
        if offset_matches:
            offset_seconds = int(offset_matches[-1].group("milliseconds")) / 1000.0

    parsed: list[tuple[LRCLine, int]] = []
    for source_line, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.lstrip("\ufeff").strip()
        position = 0
        timestamps: list[float] = []
        while match := _TIMESTAMP_RE.match(line, position):
            minutes = int(match.group("minutes"))
            seconds = int(match.group("seconds"))
            fraction_text = match.group("fraction") or ""
            fraction = int(fraction_text) / (10 ** len(fraction_text)) if fraction_text else 0.0
            timestamps.append(minutes * 60.0 + seconds + fraction + offset_seconds)
            position = match.end()

        if not timestamps:
            continue

        lyric = line[position:].strip()
        if not lyric:
            continue

        for timestamp_order, timestamp in enumerate(timestamps):
            parsed.append(
                (
                    LRCLine(
                        timestamp=max(0.0, timestamp),
                        text=lyric,
                        source_line=source_line,
                    ),
                    timestamp_order,
                )
            )

    # Playback order is the meaningful lyric order, including multi-timestamp LRC.
    parsed.sort(key=lambda item: (item[0].timestamp, item[0].source_line, item[1]))
    return [line for line, _ in parsed]


def load_lrc(path: str | Path, *, apply_offset_tag: bool = True) -> list[LRCLine]:
    """Load a UTF-8 LRC file and return its timestamped lyric lines."""

    content = Path(path).expanduser().read_text(encoding="utf-8-sig")
    return parse_lrc(content, apply_offset_tag=apply_offset_tag)


parse_lrc_file = load_lrc


def normalize_lyric(text: str) -> str:
    """Normalize lyric text for alignment and character error measurement."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    characters: list[str] = []
    for character in normalized:
        if character.isspace():
            continue
        if character == "’":
            character = "'"
        category = unicodedata.category(character)
        if category[0] in {"P", "S"} and character not in _PRESERVED_MATCH_SYMBOLS:
            continue
        characters.append(character)
    return "".join(characters)


def levenshtein_distance(left: str, right: str) -> int:
    """Return character edit distance, with a standard-library fallback."""

    if _RapidLevenshtein is not None:
        return int(_RapidLevenshtein.distance(left, right))

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) > len(right):
        left, right = right, left

    previous = list(range(len(left) + 1))
    for row, right_character in enumerate(right, start=1):
        current = [row]
        for column, left_character in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(reference: str, candidate: str) -> float:
    """Return CER after the same normalization used for line alignment."""

    normalized_reference = normalize_lyric(reference)
    normalized_candidate = normalize_lyric(candidate)
    if not normalized_reference:
        return 0.0 if not normalized_candidate else 1.0
    return levenshtein_distance(normalized_reference, normalized_candidate) / len(normalized_reference)


def lyric_similarity(reference: str, candidate: str) -> float:
    """Return normalized character similarity in the inclusive range 0..1."""

    normalized_reference = normalize_lyric(reference)
    normalized_candidate = normalize_lyric(candidate)
    longest = max(len(normalized_reference), len(normalized_candidate))
    if longest == 0:
        return 1.0
    return 1.0 - (
        levenshtein_distance(normalized_reference, normalized_candidate) / longest
    )


def _coerce_line(value: LRCLine | Mapping[str, Any], index: int) -> LRCLine:
    if isinstance(value, LRCLine):
        timestamp = value.timestamp
        text = value.text
        source_line = value.source_line
    elif not isinstance(value, Mapping):
        raise TypeError("lines must contain LRCLine objects or mappings")
    else:
        timestamp_keys = ("timestamp", "time", "start_time", "start_time_offset", "start_time_raw")
        timestamp = None
        for key in timestamp_keys:
            if key in value:
                timestamp = value[key]
                break
        if timestamp is None or "text" not in value:
            raise ValueError("line mappings require text and a timestamp field")
        text = value["text"]
        source_line = value.get("source_line", index + 1)
    parsed_timestamp = float(timestamp)
    if not math.isfinite(parsed_timestamp) or parsed_timestamp < 0:
        raise ValueError("line timestamps must be finite and non-negative")
    return LRCLine(parsed_timestamp, str(text), int(source_line))


def _coerce_lines(lines: Sequence[LRCLine | Mapping[str, Any]]) -> list[LRCLine]:
    return [_coerce_line(line, index) for index, line in enumerate(lines)]


def align_lines(
    reference_lines: Sequence[LRCLine | Mapping[str, Any]],
    candidate_lines: Sequence[LRCLine | Mapping[str, Any]],
    *,
    min_similarity: float = 0.55,
    gap_penalty: float = 0.35,
) -> list[LineAlignment]:
    """Globally align lyric lines while preserving order and one-to-one use.

    Dynamic programming considers unmatched lines on either side. Relative line
    position breaks otherwise identical text-score ties, which makes repeated
    choruses deterministic without letting timestamps affect the alignment.
    """

    if not math.isfinite(min_similarity) or not 0.0 <= min_similarity <= 1.0:
        raise ValueError("min_similarity must be between 0 and 1")
    if not math.isfinite(gap_penalty) or gap_penalty < 0.0:
        raise ValueError("gap_penalty must be non-negative")

    references = _coerce_lines(reference_lines)
    candidates = _coerce_lines(candidate_lines)
    reference_count = len(references)
    candidate_count = len(candidates)

    normalized_references = [normalize_lyric(line.text) for line in references]
    normalized_candidates = [normalize_lyric(line.text) for line in candidates]
    distances = [
        [levenshtein_distance(reference, candidate) for candidate in normalized_candidates]
        for reference in normalized_references
    ]
    similarities = [
        [
            1.0 - distance / max(len(normalized_references[i]), len(normalized_candidates[j]), 1)
            for j, distance in enumerate(row)
        ]
        for i, row in enumerate(distances)
    ]

    table: list[list[_DPCell]] = [
        [
            _DPCell(-math.inf, 0, 0, 0.0, 0, None, None)
            for _ in range(candidate_count + 1)
        ]
        for _ in range(reference_count + 1)
    ]
    table[0][0] = _DPCell(0.0, 0, 0, 0.0, 0, None, None)
    for i in range(1, reference_count + 1):
        previous = table[i - 1][0]
        table[i][0] = _DPCell(
            previous.score - gap_penalty,
            previous.matches,
            previous.exact_matches,
            previous.position_cost,
            previous.gaps + 1,
            (i - 1, 0),
            "skip_reference",
        )
    for j in range(1, candidate_count + 1):
        previous = table[0][j - 1]
        table[0][j] = _DPCell(
            previous.score - gap_penalty,
            previous.matches,
            previous.exact_matches,
            previous.position_cost,
            previous.gaps + 1,
            (0, j - 1),
            "skip_candidate",
        )

    for i in range(1, reference_count + 1):
        for j in range(1, candidate_count + 1):
            options: list[_DPCell] = []
            similarity = similarities[i - 1][j - 1]
            if similarity >= min_similarity and normalized_references[i - 1] and normalized_candidates[j - 1]:
                previous = table[i - 1][j - 1]
                relative_reference = (i - 0.5) / max(reference_count, 1)
                relative_candidate = (j - 0.5) / max(candidate_count, 1)
                options.append(
                    _DPCell(
                        previous.score + similarity,
                        previous.matches + 1,
                        previous.exact_matches + (distances[i - 1][j - 1] == 0),
                        previous.position_cost + abs(relative_reference - relative_candidate),
                        previous.gaps,
                        (i - 1, j - 1),
                        "match",
                    )
                )

            previous = table[i - 1][j]
            options.append(
                _DPCell(
                    previous.score - gap_penalty,
                    previous.matches,
                    previous.exact_matches,
                    previous.position_cost,
                    previous.gaps + 1,
                    (i - 1, j),
                    "skip_reference",
                )
            )
            previous = table[i][j - 1]
            options.append(
                _DPCell(
                    previous.score - gap_penalty,
                    previous.matches,
                    previous.exact_matches,
                    previous.position_cost,
                    previous.gaps + 1,
                    (i, j - 1),
                    "skip_candidate",
                )
            )
            table[i][j] = max(options, key=_DPCell.key)

    alignments: list[LineAlignment] = []
    i, j = reference_count, candidate_count
    while i or j:
        cell = table[i][j]
        if cell.operation == "match":
            alignments.append(
                LineAlignment(
                    reference_index=i - 1,
                    candidate_index=j - 1,
                    similarity=similarities[i - 1][j - 1],
                    edit_distance=distances[i - 1][j - 1],
                )
            )
        if cell.previous is None:  # Defensive guard for a malformed DP table.
            raise RuntimeError("failed to reconstruct lyric alignment")
        i, j = cell.previous

    alignments.reverse()
    return alignments


align_lyrics = align_lines


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _clean_number(value: float | None) -> float | None:
    if value is None:
        return None
    if abs(value) < 0.0000005:
        return 0.0
    return round(float(value), 6)


def _time_statistics(errors: Sequence[float]) -> dict[str, float | None]:
    absolute_errors = [abs(error) for error in errors]
    return {
        "median_signed_s": _clean_number(statistics.median(errors) if errors else None),
        "median_abs_s": _clean_number(statistics.median(absolute_errors) if absolute_errors else None),
        "p90_abs_s": _clean_number(_percentile(absolute_errors, 0.90)),
    }


def evaluate_lines(
    reference_lines: Sequence[LRCLine | Mapping[str, Any]],
    candidate_lines: Sequence[LRCLine | Mapping[str, Any]],
    *,
    min_similarity: float = 0.55,
    gap_penalty: float = 0.35,
    time_tolerance_s: float = 0.80,
) -> dict[str, Any]:
    """Evaluate parsed lines and return a report containing only JSON types.

    ``manual_edit_rate`` counts every human edit against the number of reference
    lines: a missing reference line, an extra candidate line, or a matched line
    with a text error or residual error above ``time_tolerance_s``. It can exceed
    1.0 when noisy output contains many extra lines.
    """

    if not math.isfinite(time_tolerance_s) or time_tolerance_s < 0.0:
        raise ValueError("time_tolerance_s must be non-negative")

    references = _coerce_lines(reference_lines)
    candidates = _coerce_lines(candidate_lines)
    alignments = align_lines(
        references,
        candidates,
        min_similarity=min_similarity,
        gap_penalty=gap_penalty,
    )

    raw_errors = [
        candidates[alignment.candidate_index].timestamp
        - references[alignment.reference_index].timestamp
        for alignment in alignments
    ]
    estimated_offset = statistics.median([-error for error in raw_errors]) if raw_errors else None
    residual_errors = (
        [error + estimated_offset for error in raw_errors]
        if estimated_offset is not None
        else []
    )

    matched_reference_indexes = {alignment.reference_index for alignment in alignments}
    matched_candidate_indexes = {alignment.candidate_index for alignment in alignments}
    unmatched_reference_indexes = [
        index for index in range(len(references)) if index not in matched_reference_indexes
    ]
    unmatched_candidate_indexes = [
        index for index in range(len(candidates)) if index not in matched_candidate_indexes
    ]

    match_reports: list[dict[str, Any]] = []
    total_edit_distance = 0
    total_reference_characters = 0
    matched_manual_edits = 0
    for alignment, raw_error, residual_error in zip(alignments, raw_errors, residual_errors):
        reference = references[alignment.reference_index]
        candidate = candidates[alignment.candidate_index]
        normalized_reference = normalize_lyric(reference.text)
        normalized_candidate = normalize_lyric(candidate.text)
        text_edit_required = normalized_reference != normalized_candidate
        time_edit_required = abs(residual_error) > time_tolerance_s
        reasons: list[str] = []
        if text_edit_required:
            reasons.append("text")
        if time_edit_required:
            reasons.append("time")
        manual_edit_required = bool(reasons)
        matched_manual_edits += manual_edit_required
        total_edit_distance += alignment.edit_distance
        total_reference_characters += len(normalized_reference)

        match_reports.append(
            {
                "reference_index": alignment.reference_index,
                "candidate_index": alignment.candidate_index,
                "reference_text": reference.text,
                "candidate_text": candidate.text,
                "reference_time_s": _clean_number(reference.timestamp),
                "candidate_time_s": _clean_number(candidate.timestamp),
                "similarity": _clean_number(alignment.similarity),
                "edit_distance": alignment.edit_distance,
                "reference_characters": len(normalized_reference),
                "raw_time_error_s": _clean_number(raw_error),
                "raw_abs_time_error_s": _clean_number(abs(raw_error)),
                "residual_time_error_s": _clean_number(residual_error),
                "residual_abs_time_error_s": _clean_number(abs(residual_error)),
                "manual_edit_required": manual_edit_required,
                "manual_edit_reasons": reasons,
            }
        )

    unmatched_references = [
        {
            "reference_index": index,
            "text": references[index].text,
            "time_s": _clean_number(references[index].timestamp),
        }
        for index in unmatched_reference_indexes
    ]
    unmatched_candidates = [
        {
            "candidate_index": index,
            "text": candidates[index].text,
            "time_s": _clean_number(candidates[index].timestamp),
        }
        for index in unmatched_candidate_indexes
    ]

    reference_count = len(references)
    candidate_count = len(candidates)
    matched_count = len(alignments)
    line_recall = matched_count / reference_count if reference_count else (1.0 if not candidates else 0.0)
    line_precision = matched_count / candidate_count if candidate_count else (1.0 if not references else 0.0)
    cer = total_edit_distance / total_reference_characters if total_reference_characters else 0.0
    manual_edit_count = matched_manual_edits + len(unmatched_references) + len(unmatched_candidates)
    alignment_operation_count = matched_count + len(unmatched_references) + len(unmatched_candidates)
    if reference_count:
        manual_edit_rate = manual_edit_count / reference_count
    else:
        manual_edit_rate = float(manual_edit_count > 0)

    return {
        "schema_version": 1,
        "reference_line_count": reference_count,
        "candidate_line_count": candidate_count,
        "matched_line_count": matched_count,
        "unmatched_reference_count": len(unmatched_references),
        "unmatched_candidate_count": len(unmatched_candidates),
        "line_recall": _clean_number(line_recall),
        "line_precision": _clean_number(line_precision),
        "cer": _clean_number(cer),
        "estimated_offset_s": _clean_number(estimated_offset),
        "estimated_offset_ms": _clean_number(
            estimated_offset * 1000.0 if estimated_offset is not None else None
        ),
        "raw_time_error": _time_statistics(raw_errors),
        "residual_time_error": _time_statistics(residual_errors),
        "manual_edit_count": manual_edit_count,
        "manual_edit_denominator": reference_count,
        "alignment_operation_count": alignment_operation_count,
        "manual_edit_rate": _clean_number(manual_edit_rate),
        "settings": {
            "min_similarity": min_similarity,
            "gap_penalty": gap_penalty,
            "time_tolerance_s": time_tolerance_s,
        },
        "matches": match_reports,
        "unmatched_reference": unmatched_references,
        "unmatched_candidate": unmatched_candidates,
    }


def evaluate_lrc(
    reference_lrc: str,
    candidate_lrc: str,
    *,
    min_similarity: float = 0.55,
    gap_penalty: float = 0.35,
    time_tolerance_s: float = 0.80,
    apply_offset_tags: bool = True,
) -> dict[str, Any]:
    """Parse and evaluate two in-memory LRC documents."""

    references = parse_lrc(reference_lrc, apply_offset_tag=apply_offset_tags)
    candidates = parse_lrc(candidate_lrc, apply_offset_tag=apply_offset_tags)
    return evaluate_lines(
        references,
        candidates,
        min_similarity=min_similarity,
        gap_penalty=gap_penalty,
        time_tolerance_s=time_tolerance_s,
    )


def evaluate_files(
    reference_path: str | Path,
    candidate_path: str | Path,
    *,
    min_similarity: float = 0.55,
    gap_penalty: float = 0.35,
    time_tolerance_s: float = 0.80,
    apply_offset_tags: bool = True,
) -> dict[str, Any]:
    """Evaluate two LRC files and return a JSON-compatible report."""

    references = load_lrc(reference_path, apply_offset_tag=apply_offset_tags)
    candidates = load_lrc(candidate_path, apply_offset_tag=apply_offset_tags)
    return evaluate_lines(
        references,
        candidates,
        min_similarity=min_similarity,
        gap_penalty=gap_penalty,
        time_tolerance_s=time_tolerance_s,
    )


def report_json(report: Mapping[str, Any], *, pretty: bool = True) -> str:
    """Serialize a report for a CLI, log, or cache file."""

    return json.dumps(
        report,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=False,
        allow_nan=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按歌词顺序对齐参考 LRC 与候选 LRC，并输出 JSON 评估报告。"
    )
    parser.add_argument("reference", type=Path, help="参考 LRC 文件")
    parser.add_argument("candidate", type=Path, help="待评估 LRC 文件")
    parser.add_argument("-o", "--output", type=Path, help="报告路径；默认输出到 stdout")
    parser.add_argument("--min-similarity", type=float, default=0.55, help="行匹配最低相似度")
    parser.add_argument("--gap-penalty", type=float, default=0.35, help="漏行或多行的 DP 惩罚")
    parser.add_argument(
        "--time-tolerance",
        type=float,
        default=0.80,
        metavar="SECONDS",
        help="全局 offset 后无需人工调时的最大绝对误差",
    )
    parser.add_argument("--ignore-offset-tags", action="store_true", help="不应用 LRC 的 [offset:] tag")
    parser.add_argument("--compact", action="store_true", help="输出紧凑 JSON")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有报告文件")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        reference_path = args.reference.expanduser().resolve()
        candidate_path = args.candidate.expanduser().resolve()
        resolved_output = args.output.expanduser().resolve() if args.output else None
        if resolved_output:
            protected_inputs = {reference_path, candidate_path}
            if resolved_output in protected_inputs:
                raise ValueError("report output must not overwrite a reference or candidate LRC")
            if resolved_output.exists() and not args.force:
                raise FileExistsError(
                    f"report already exists: {resolved_output}; use --force to overwrite"
                )
        report = evaluate_files(
            reference_path,
            candidate_path,
            min_similarity=args.min_similarity,
            gap_penalty=args.gap_penalty,
            time_tolerance_s=args.time_tolerance,
            apply_offset_tags=not args.ignore_offset_tags,
        )
        rendered = report_json(report, pretty=not args.compact)
        if resolved_output:
            atomic_write_text(
                resolved_output,
                rendered + "\n",
                encoding="utf-8",
                overwrite=args.force,
            )
        else:
            print(rendered)
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
