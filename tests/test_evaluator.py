import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from evaluator import (
    LRCLine,
    align_lines,
    character_error_rate,
    evaluate_files,
    evaluate_lrc,
    main,
    parse_lrc,
)


class ParseLRCTests(unittest.TestCase):
    def test_parses_pure_and_tagged_lrc_with_offset(self) -> None:
        content = """\
\ufeff[ti:测试歌曲]
[ar:测试歌手]
[offset:+250]
[00:01.00]第一句
[00:02.5][00:05.500]副歌
"""

        lines = parse_lrc(content)

        self.assertEqual([line.text for line in lines], ["第一句", "副歌", "副歌"])
        self.assertEqual([line.timestamp for line in lines], [1.25, 2.75, 5.75])
        self.assertEqual([line.source_line for line in lines], [4, 5, 5])

    def test_ignores_metadata_and_empty_timestamped_lines(self) -> None:
        lines = parse_lrc("[ti:标题]\n[by:制作者]\n[00:00.00]\n[00:03.00]歌词\n")
        self.assertEqual(lines, [LRCLine(timestamp=3.0, text="歌词", source_line=4)])


class AlignmentTests(unittest.TestCase):
    def test_repeated_chorus_is_monotonic_and_one_to_one(self) -> None:
        reference = [
            LRCLine(0.0, "开场"),
            LRCLine(10.0, "同一句副歌"),
            LRCLine(20.0, "第一段结束"),
            LRCLine(30.0, "同一句副歌"),
            LRCLine(40.0, "全曲结束"),
        ]
        candidate = [
            LRCLine(0.2, "开场"),
            LRCLine(10.2, "同一句副歌"),
            LRCLine(20.2, "第一段结東"),
            LRCLine(25.0, "视频水印"),
            LRCLine(30.2, "同一句副歌"),
            LRCLine(40.2, "全曲结束"),
        ]

        alignments = align_lines(reference, candidate)
        pairs = [(item.reference_index, item.candidate_index) for item in alignments]

        self.assertEqual(pairs, [(0, 0), (1, 1), (2, 2), (3, 4), (4, 5)])
        self.assertEqual(len({item.reference_index for item in alignments}), len(alignments))
        self.assertEqual(len({item.candidate_index for item in alignments}), len(alignments))


class EvaluationTests(unittest.TestCase):
    def test_estimates_global_offset_and_reports_zero_residual(self) -> None:
        reference = """\
[00:10.00]第一句
[00:20.00]第二句
[00:30.00]第三句
"""
        candidate = """\
[00:09.50]第一句
[00:19.50]第二句
[00:29.50]第三句
"""

        report = evaluate_lrc(reference, candidate)

        self.assertEqual(report["estimated_offset_s"], 0.5)
        self.assertEqual(report["estimated_offset_ms"], 500.0)
        self.assertEqual(report["raw_time_error"]["median_signed_s"], -0.5)
        self.assertEqual(report["raw_time_error"]["median_abs_s"], 0.5)
        self.assertEqual(report["residual_time_error"]["median_abs_s"], 0.0)
        self.assertEqual(report["residual_time_error"]["p90_abs_s"], 0.0)
        self.assertTrue(all(match["raw_time_error_s"] == -0.5 for match in report["matches"]))

    def test_residual_p90_uses_absolute_errors_after_median_offset(self) -> None:
        reference = """\
[00:10.00]第一句
[00:20.00]第二句
[00:30.00]第三句
"""
        candidate = """\
[00:09.50]第一句
[00:19.50]第二句
[00:29.00]第三句
"""

        report = evaluate_lrc(reference, candidate, time_tolerance_s=0.30)

        self.assertEqual(report["estimated_offset_s"], 0.5)
        self.assertEqual(report["residual_time_error"]["median_abs_s"], 0.0)
        self.assertEqual(report["residual_time_error"]["p90_abs_s"], 0.4)
        self.assertEqual(report["manual_edit_count"], 1)

    def test_typo_contributes_to_cer_and_manual_edit_rate(self) -> None:
        reference = """\
[00:10.00]你的美总在两个时代
[00:20.00]另外一句完全正确
"""
        candidate = """\
[00:10.00]你的美总在两个时伐
[00:20.00]另外一句完全正确
"""

        report = evaluate_lrc(reference, candidate)

        expected_cer = 1 / (len("你的美总在两个时代") + len("另外一句完全正确"))
        self.assertAlmostEqual(report["cer"], expected_cer, places=6)
        self.assertEqual(report["line_precision"], 1.0)
        self.assertEqual(report["line_recall"], 1.0)
        self.assertEqual(report["manual_edit_count"], 1)
        self.assertEqual(report["manual_edit_rate"], 0.5)
        self.assertEqual(report["matches"][0]["manual_edit_reasons"], ["text"])
        self.assertAlmostEqual(
            character_error_rate("你的美总在两个时代", "你的美总在两个时伐"),
            1 / len("你的美总在两个时代"),
        )

    def test_precision_recall_and_unmatched_lines_are_reported(self) -> None:
        reference = "[00:01.00]甲乙丙丁\n[00:02.00]完全不同的参考行\n"
        candidate = "[00:01.00]甲乙丙丁\n[00:03.00]视频平台水印\n"

        report = evaluate_lrc(reference, candidate)

        self.assertEqual(report["matched_line_count"], 1)
        self.assertEqual(report["line_recall"], 0.5)
        self.assertEqual(report["line_precision"], 0.5)
        self.assertEqual(report["unmatched_reference_count"], 1)
        self.assertEqual(report["unmatched_candidate_count"], 1)
        self.assertEqual(report["manual_edit_rate"], 1.0)
        self.assertEqual(report["manual_edit_denominator"], 2)
        json.dumps(report, ensure_ascii=False)

    def test_no_matches_has_unknown_offset(self) -> None:
        report = evaluate_lrc(
            "[00:01.00]完全不同的参考歌词\n",
            "[00:02.00]视频平台水印内容\n",
        )

        self.assertEqual(report["matched_line_count"], 0)
        self.assertIsNone(report["estimated_offset_s"])
        self.assertIsNone(report["estimated_offset_ms"])
        self.assertIsNone(report["raw_time_error"]["median_abs_s"])
        self.assertIsNone(report["residual_time_error"]["p90_abs_s"])

    def test_file_api_returns_json_compatible_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.lrc"
            candidate = root / "candidate.lrc"
            reference.write_text("[00:01.00]歌词\n", encoding="utf-8")
            candidate.write_text("[00:01.10]歌词\n", encoding="utf-8")

            report = evaluate_files(reference, candidate)

        self.assertEqual(report["matched_line_count"], 1)
        self.assertIsInstance(json.dumps(report, ensure_ascii=False), str)

    def test_cli_prints_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.lrc"
            candidate = root / "candidate.lrc"
            reference.write_text("[00:01.00]歌词\n", encoding="utf-8")
            candidate.write_text("[00:01.10]歌词\n", encoding="utf-8")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main([str(reference), str(candidate), "--compact"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["matched_line_count"], 1)

    def test_rejects_non_finite_settings(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_lrc("[00:01.00]歌词\n", "[00:01.00]歌词\n", gap_penalty=math.nan)
        with self.assertRaises(ValueError):
            evaluate_lrc(
                "[00:01.00]歌词\n",
                "[00:01.00]歌词\n",
                time_tolerance_s=math.inf,
            )

    def test_rejects_non_finite_lrc_line_objects(self) -> None:
        with self.assertRaises(ValueError):
            align_lines([LRCLine(math.nan, "歌词")], [LRCLine(1.0, "歌词")])
        with self.assertRaises(ValueError):
            align_lines([LRCLine(-1.0, "歌词")], [LRCLine(1.0, "歌词")])

    def test_cli_refuses_to_overwrite_input_or_existing_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.lrc"
            candidate = root / "candidate.lrc"
            report = root / "report.json"
            reference.write_text("[00:01.00]歌词\n", encoding="utf-8")
            candidate.write_text("[00:01.00]歌词\n", encoding="utf-8")
            report.write_text("keep", encoding="utf-8")

            with self.assertRaises(SystemExit):
                main([str(reference), str(candidate), "-o", str(reference)])
            with self.assertRaises(SystemExit):
                main([str(reference), str(candidate), "-o", str(report)])
            self.assertEqual(reference.read_text(encoding="utf-8"), "[00:01.00]歌词\n")
            self.assertEqual(report.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
