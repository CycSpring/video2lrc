from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QProcess

from video2lrc_ui.models import ROI, JobSpec
from video2lrc_ui.process_runner import (
    WINDOWS_CREATE_NO_WINDOW,
    ProcessRunner,
    build_process_invocation,
    configure_windows_no_window,
    is_routine_rapidocr_empty_warning,
    strip_ansi_sequences,
)


def test_roi_parses_formats_and_stays_inside_frame() -> None:
    roi = ROI.parse("0.18, 0.76, 0.78, 0.22")

    assert roi.as_tuple() == (0.18, 0.76, 0.78, 0.22)
    assert roi.to_cli_value() == "0.18,0.76,0.78,0.22"


@pytest.mark.parametrize(
    "value",
    [
        "0.8,0.2,0.3,0.4",
        "0.1,0.9,0.8,0.2",
        "0,0,0,1",
        "nan,0,1,1",
        "0,0,1",
    ],
)
def test_roi_rejects_invalid_coordinates(value: str) -> None:
    with pytest.raises(ValueError):
        ROI.parse(value)


def test_job_spec_builds_shell_free_args_for_special_windows_paths() -> None:
    video = Path(r"D:\媒体 & 项目\示例 视频.mp4")
    output = Path(r"D:\媒体 & 项目\示例 歌词.lrc")
    cancel = Path(r"C:\临时 & 状态\cancel.request")
    spec = JobSpec(
        video_path=video,
        output_path=output,
        roi=ROI(0.18, 0.76, 0.78, 0.22),
        fps=4,
        workers=3,
        resume=True,
        force=True,
    )

    args = spec.to_cli_args(
        preview=True,
        event_stream=True,
        job_id="任务-1",
        cancel_file=cancel,
    )

    assert args[0] == str(video)
    assert args[args.index("--output") + 1] == str(output)
    assert args[args.index("--cancel-file") + 1] == str(cancel)
    assert args[args.index("--roi") + 1] == "0.18,0.76,0.78,0.22"
    assert "--preview-roi" in args
    assert "--event-stream" in args
    assert "--resume" in args
    assert "--force" in args
    assert not any('"' in item for item in args)


def test_job_spec_uses_default_output_without_forcing_an_output_argument() -> None:
    spec = JobSpec(r"D:\视频\测试.mp4")

    assert spec.default_output_path == Path(r"D:\视频\测试.lrc")
    assert "--output" not in spec.to_cli_args()


def test_job_spec_normalizes_all_paths_before_building_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    spec = JobSpec(
        "input/song.mp4",
        output_path="output/song.lrc",
        workdir="cache",
    )

    args = spec.to_cli_args(cancel_file="state/cancel.request")

    assert spec.video_path == (tmp_path / "input/song.mp4").resolve()
    assert spec.output_path == (tmp_path / "output/song.lrc").resolve()
    assert spec.workdir == (tmp_path / "cache").resolve()
    assert args[0] == str(spec.video_path)
    assert args[args.index("--output") + 1] == str(spec.output_path)
    assert args[args.index("--workdir") + 1] == str(spec.workdir)
    assert args[args.index("--cancel-file") + 1] == str(
        (tmp_path / "state/cancel.request").resolve()
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"fps": math.inf}, "fps"),
        ({"workers": 0}, "workers"),
        ({"confidence_threshold": 1.1}, "confidence_threshold"),
        ({"same_threshold": -1}, "same_threshold"),
        ({"switch_confirm_frames": 0}, "switch_confirm_frames"),
        ({"min_line_gap_ms": -1}, "min_line_gap_ms"),
        ({"style": "unknown"}, "style"),
    ],
)
def test_job_spec_rejects_invalid_numeric_and_choice_values(
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        JobSpec("song.mp4", **kwargs)


def test_job_spec_rejects_mutually_exclusive_roi_modes() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        JobSpec("song.mp4", roi=ROI(0, 0.5, 1, 0.5), crop_bottom_ratio=0.4)


def test_job_spec_rejects_output_equal_to_input() -> None:
    with pytest.raises(ValueError, match="output_path"):
        JobSpec("song.mp4", output_path="song.mp4")


def test_development_invocation_uses_unbuffered_python_and_main_script(tmp_path) -> None:
    python = tmp_path / "venv with spaces" / "python.exe"
    cancel = tmp_path / "取消 & stop.request"
    spec = JobSpec(r"D:\媒体 & 项目\视频.mp4")

    program, args, working_directory = build_process_invocation(
        spec,
        job_id="job-1",
        cancel_file=cancel,
        project_root=tmp_path,
        frozen=False,
        executable=python,
    )

    assert program == str(python.resolve())
    assert args[:3] == ["-u", str(tmp_path.resolve() / "main.py"), str(spec.video_path)]
    assert args[args.index("--cancel-file") + 1] == str(cancel)
    assert working_directory == str(tmp_path.resolve())


def test_frozen_invocation_uses_sibling_cli_executable(tmp_path) -> None:
    gui_executable = tmp_path / "Video2LRC UI.exe"

    program, args, working_directory = build_process_invocation(
        JobSpec("song.mp4"),
        preview=True,
        job_id="job-2",
        cancel_file=tmp_path / "cancel.request",
        frozen=True,
        executable=gui_executable,
    )

    assert program == str(tmp_path.resolve() / "video2lrc-cli.exe")
    assert args[0] == str(Path("song.mp4").resolve())
    assert "main.py" not in " ".join(args)
    assert "--preview-roi" in args
    assert working_directory == str(tmp_path.resolve())


def test_windows_process_modifier_adds_no_window_without_touching_pipes() -> None:
    class FakeProcess:
        modifier = None

        def setCreateProcessArgumentsModifier(self, modifier) -> None:  # noqa: N802
            self.modifier = modifier

    process = FakeProcess()
    modifier = configure_windows_no_window(process, platform_name="nt")
    arguments = SimpleNamespace(flags=0x00000400)

    assert modifier is process.modifier
    modifier(arguments)
    assert arguments.flags == 0x00000400 | WINDOWS_CREATE_NO_WINDOW

    runner = ProcessRunner()
    assert (
        runner._process.processChannelMode()
        == QProcess.ProcessChannelMode.SeparateChannels
    )


def test_process_log_helpers_strip_ansi_and_classify_only_routine_empty_warnings() -> None:
    empty_warning = (
        "\x1b[33m[WARNING] 2026-07-15 [RapidOCR] main.py:132: "
        "The text detection result is empty\x1b[0m"
    )

    assert strip_ansi_sequences(empty_warning) == (
        "[WARNING] 2026-07-15 [RapidOCR] main.py:132: "
        "The text detection result is empty"
    )
    assert is_routine_rapidocr_empty_warning(empty_warning) is True
    assert (
        is_routine_rapidocr_empty_warning(
            "[WARNING] 2026-07-15 [RapidOCR] model download failed"
        )
        is False
    )
    assert (
        is_routine_rapidocr_empty_warning(
            "[WARNING] another component: The text detection result is empty"
        )
        is False
    )


def test_process_runner_collapses_empty_frame_warnings_and_preserves_other_logs() -> None:
    runner = ProcessRunner()
    logs: list[str] = []
    runner.log_received.connect(logs.append)

    runner._append_stderr(
        "\x1b[33m[WARNING] 2026-07-15 [RapidOCR] main.py:132: "
        "The text detection result is empty\x1b[0m\n"
        "\x1b[33m[WARNING] 2026-07-15 [RapidOCR] output.py:39: "
        "The identified content is empty.\x1b[0m\n"
        "\x1b[33m[WARNING] 2026-07-15 [RapidOCR] low confidence\x1b[0m\n"
    )

    assert logs == ["[WARNING] 2026-07-15 [RapidOCR] low confidence"]
    assert "\x1b" not in runner._stderr_tail
    assert "text detection result is empty" not in runner._stderr_tail

    runner._emit_suppressed_warning_summary()

    assert logs[-1] == (
        "OCR 提示：已折叠 2 条“未检测到字幕文字”的重复日志；"
        "转场或无字幕画面通常属于正常情况。"
    )
    assert runner._suppressed_empty_ocr_warnings == 0
