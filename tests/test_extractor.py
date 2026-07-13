from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

import extractor


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_ensure_media_tools_uses_argument_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(
        extractor.shutil,
        "which",
        lambda command: str(Path("tools") / f"{command}.exe"),
    )

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return _completed(stdout=f"{Path(command[0]).stem} version 7.1\n")

    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    result = extractor.ensure_media_tools()

    assert set(result) == {"ffmpeg", "ffprobe"}
    assert result["ffmpeg"]["version"].endswith("version 7.1")
    assert [call[0][1:] for call in calls] == [["-version"], ["-version"]]
    assert all(isinstance(call[0], list) for call in calls)
    assert all("shell" not in call[1] for call in calls)


def test_missing_media_command_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor.shutil, "which", lambda _command: None)

    with pytest.raises(extractor.MediaToolError, match="ffmpeg") as error:
        extractor.ensure_media_tools()

    assert "PATH" in str(error.value)


def test_frozen_cli_resolves_bundled_media_tool_before_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    internal_root = tmp_path / "_internal"
    bundled_tool = internal_root / "bin" / "ffmpeg.exe"
    bundled_tool.parent.mkdir(parents=True)
    bundled_tool.write_bytes(b"executable")

    monkeypatch.setattr(extractor.sys, "frozen", True, raising=False)
    monkeypatch.setattr(extractor.sys, "_MEIPASS", str(internal_root), raising=False)
    monkeypatch.setattr(extractor.shutil, "which", lambda _command: None)

    assert extractor._resolve_executable("ffmpeg") == str(bundled_tool.resolve())


def test_probe_video_handles_rotation_duration_and_unicode_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "中文 视频.mp4"
    video.write_bytes(b"video")
    payload = {
        "streams": [
            {"index": 0, "codec_type": "audio"},
            {
                "index": 1,
                "codec_type": "video",
                "width": 600,
                "height": 600,
                "disposition": {"attached_pic": 1},
            },
            {
                "index": 2,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
                "start_time": "1.250000",
                "side_data_list": [{"rotation": -90}],
            },
        ],
        "format": {"duration": "123.456", "start_time": "0.5"},
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(extractor, "_resolve_executable", lambda _command: "ffprobe.exe")

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _completed(stdout=json.dumps(payload))

    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    info = extractor.probe_video(video)

    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["stream_index"] == 2
    assert (info["display_width"], info["display_height"]) == (1080, 1920)
    assert info["rotation"] == 270
    assert info["rotation_raw"] == -90
    assert info["duration"] == pytest.approx(123.456)
    assert info["duration_source"] == "format"
    assert info["start_time"] == pytest.approx(1.25)
    assert info["average_frame_rate"] == pytest.approx(30000 / 1001)
    command = captured["command"]
    assert isinstance(command, list)
    assert command[-1] == str(video.resolve())
    assert "中文 视频.mp4" in command[-1]
    assert captured["kwargs"]["check"] is False


def test_probe_video_rejects_missing_video_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "audio.m4a"
    video.write_bytes(b"audio")
    monkeypatch.setattr(extractor, "_resolve_executable", lambda _command: "ffprobe.exe")
    monkeypatch.setattr(
        extractor.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(
            stdout=json.dumps({"streams": [{"codec_type": "audio"}]})
        ),
    )

    with pytest.raises(extractor.VideoProbeError, match="no video stream"):
        extractor.probe_video(video)


def test_resolve_normalized_and_bottom_roi_after_rotation() -> None:
    info = {"display_width": 1080, "display_height": 1920}

    bottom = extractor.resolve_roi(info, crop_bottom_ratio=0.4)
    explicit = extractor.resolve_roi(info, roi="0.1,0.5,0.8,0.25", crop_bottom_ratio=None)

    assert bottom["normalized"] == {"x": 0.0, "y": 0.6, "width": 1.0, "height": 0.4}
    assert bottom["pixels"] == {"x": 0, "y": 1152, "width": 1080, "height": 768}
    assert explicit["pixels"] == {"x": 108, "y": 960, "width": 864, "height": 480}
    with pytest.raises(extractor.ROIError, match="inside"):
        extractor.resolve_roi(info, roi=(0.8, 0.5, 0.3, 0.4), crop_bottom_ratio=None)
    with pytest.raises(extractor.ROIError, match="mutually exclusive"):
        extractor.resolve_roi(info, roi=(0, 0, 1, 1), crop_bottom_ratio=0.5)


def test_filter_and_frame_index_use_exact_fixed_fps(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    for name in ("000001.png", "000002.png", "000100.png"):
        (frames / name).write_bytes(b"png")
    roi = {
        "pixels": {"x": 0, "y": 648, "width": 1920, "height": 432}
    }

    assert extractor.build_filter_chain(roi, 4) == (
        "setpts=PTS-STARTPTS,crop=1920:432:0:648,fps=4"
    )
    assert extractor.frame_time_fraction(100, 4) == Fraction(99, 4)
    index = extractor.build_frame_index(frames, 4)
    assert [item["time"] for item in index] == [0.0, 0.25, 24.75]
    assert index[-1]["time_fraction"] == "99/4"


def test_extract_frames_returns_serializable_manifest_and_preserves_unicode_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "歌 曲 [终版].mp4"
    video.write_bytes(b"video")
    frames_dir = tmp_path / "帧 缓存"
    frames_dir.mkdir()
    (frames_dir / "000099.png").write_bytes(b"stale")
    captured: dict[str, object] = {}
    monkeypatch.setattr(extractor, "_resolve_executable", lambda _command: "ffmpeg.exe")

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        pattern = Path(command[-1])
        for number in (1, 2, 3):
            Path(str(pattern).replace("%06d", f"{number:06d}")).write_bytes(b"png")
        return _completed()

    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    result = extractor.extract_frames(
        video,
        frames_dir,
        fps=4,
        crop_bottom_ratio=0.4,
        video_info={
            "path": str(video),
            "width": 1920,
            "height": 1080,
            "display_width": 1920,
            "display_height": 1080,
            "rotation": 0,
            "duration": 1.0,
            "start_time": 7.5,
            "stream_index": 2,
        },
    )

    assert result["frame_count"] == 3
    assert [item["time"] for item in result["frame_index"]] == [0.0, 0.25, 0.5]
    assert result["filter"] == "setpts=PTS-STARTPTS,crop=1920:432:0:648,fps=4"
    assert not (frames_dir / "000099.png").exists()
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("-i") + 1] == str(video.resolve())
    assert command[command.index("-map") + 1] == "0:2"
    assert any(Path(argument).name == "歌 曲 [终版].mp4" for argument in command)
    assert command[command.index("-vf") + 1] == result["filter"]
    json.dumps(result, ensure_ascii=False)


def test_extract_frames_reports_ffmpeg_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "broken.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(extractor, "_resolve_executable", lambda _command: "ffmpeg.exe")
    monkeypatch.setattr(
        extractor.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(returncode=1, stderr="decoder failed"),
    )

    with pytest.raises(extractor.FrameExtractionError, match="decoder failed"):
        extractor.extract_frames(
            video,
            tmp_path / "frames",
            video_info={"width": 100, "height": 100},
        )


def test_extract_frames_cancellation_terminates_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    terminated: list[bool] = []

    class WaitingProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if self.returncode is None:
                raise extractor.subprocess.TimeoutExpired(["ffmpeg"], timeout)
            return "", ""

        def terminate(self) -> None:
            terminated.append(True)
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

    monkeypatch.setattr(extractor, "_resolve_executable", lambda _command: "ffmpeg.exe")
    monkeypatch.setattr(
        extractor.subprocess,
        "Popen",
        lambda *args, **kwargs: WaitingProcess(),
    )

    def cancel() -> None:
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        extractor.extract_frames(
            video,
            tmp_path / "frames",
            fps=4,
            crop_bottom_ratio=0.4,
            video_info={"width": 1920, "height": 1080, "stream_index": 0},
            cancellation_callback=cancel,
        )

    assert terminated == [True]


def test_create_roi_preview_writes_unicode_montage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    video = tmp_path / "预览 视频.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "结果 预览.jpg"

    class FakeCapture:
        def __init__(self) -> None:
            self.released = False

        def isOpened(self) -> bool:
            return True

        def set(self, _key: int, _value: float) -> bool:
            return True

        def get(self, _key: int) -> float:
            return 0.0

        def read(self) -> tuple[bool, object]:
            return True, np.full((60, 100, 3), 100, dtype=np.uint8)

        def release(self) -> None:
            self.released = True

    class CV2Proxy:
        CAP_PROP_ORIENTATION_AUTO = getattr(cv2, "CAP_PROP_ORIENTATION_AUTO", 49)
        CAP_PROP_POS_MSEC = cv2.CAP_PROP_POS_MSEC
        CAP_PROP_FRAME_COUNT = cv2.CAP_PROP_FRAME_COUNT
        CAP_PROP_FPS = cv2.CAP_PROP_FPS
        FONT_HERSHEY_SIMPLEX = cv2.FONT_HERSHEY_SIMPLEX
        LINE_AA = cv2.LINE_AA
        INTER_AREA = cv2.INTER_AREA
        ROTATE_90_COUNTERCLOCKWISE = cv2.ROTATE_90_COUNTERCLOCKWISE
        ROTATE_90_CLOCKWISE = cv2.ROTATE_90_CLOCKWISE
        ROTATE_180 = cv2.ROTATE_180
        rectangle = staticmethod(cv2.rectangle)
        putText = staticmethod(cv2.putText)
        resize = staticmethod(cv2.resize)
        rotate = staticmethod(cv2.rotate)
        imencode = staticmethod(cv2.imencode)

        @staticmethod
        def VideoCapture(_path: str) -> FakeCapture:
            return FakeCapture()

    monkeypatch.setattr(extractor, "_import_preview_dependencies", lambda: (CV2Proxy, np))
    result = extractor.create_roi_preview(
        video,
        output,
        sample_count=3,
        thumbnail_width=160,
        video_info={
            "width": 100,
            "height": 60,
            "display_width": 100,
            "display_height": 60,
            "rotation": 0,
            "duration": 10.0,
        },
    )

    assert output.is_file()
    assert output.stat().st_size > 0
    assert result["sample_count"] == 3
    assert [sample["status"] for sample in result["samples"]] == ["ok", "ok", "ok"]
    json.dumps(result, ensure_ascii=False)
