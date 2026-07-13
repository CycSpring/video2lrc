from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

import events
import main
import pipeline


def _decode_stream(value: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in value.splitlines():
        assert line.startswith(events.EVENT_PREFIX)
        records.append(json.loads(line.removeprefix(events.EVENT_PREFIX)))
    return records


def _patch_successful_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    request_cancel: Path | None = None,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "ensure_media_tools",
        lambda **_: {"ffmpeg": "test", "ffprobe": "test"},
    )
    monkeypatch.setattr(
        pipeline,
        "probe_video",
        lambda *_args, **_kwargs: {
            "width": 640,
            "height": 360,
            "rotation": 0,
            "start_time": 0.0,
            "duration": 1.0,
        },
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_roi",
        lambda *_args, **_kwargs: {
            "x": 0,
            "y": 216,
            "width": 640,
            "height": 144,
        },
    )

    def fake_extract(_video: Path, frames_dir: Path, **_kwargs: Any) -> dict[str, Any]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame_index = []
        for index in range(1, 3):
            frame = frames_dir / f"{index:06d}.png"
            frame.write_bytes(b"png")
            frame_index.append(
                {"frame": frame.name, "path": str(frame), "time": (index - 1) * 0.25}
            )
        return {"frame_index": frame_index}

    def fake_ocr(frame_index: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        results = [
            {
                **frame,
                "text": "歌词",
                "confidence": 0.99,
                "status": "ok",
            }
            for frame in frame_index
        ]
        callback = kwargs["progress_callback"]
        for current, result in enumerate(results, start=1):
            if current == 1 and request_cancel is not None:
                request_cancel.write_text("cancel", encoding="ascii")
            callback(current, len(results), result)
        return results

    monkeypatch.setattr(
        pipeline,
        "extract_frames",
        create_autospec(pipeline.extract_frames, side_effect=fake_extract),
    )
    monkeypatch.setattr(pipeline, "ocr_all_frames", fake_ocr)
    monkeypatch.setattr(
        pipeline,
        "detect_line_switches",
        lambda *_args, **_kwargs: {
            "style": "line",
            "lines": [
                {
                    "start_time_raw": 0.0,
                    "end_time": 0.5,
                    "text": "歌词",
                    "confidence": 0.99,
                    "support_frames": 2,
                    "qa_flags": [],
                }
            ],
        },
    )


def test_json_line_emitter_has_stable_envelope_and_sequence() -> None:
    stream = io.StringIO()
    emitter = events.EventEmitter(stream, job_id="job-中文")

    emitter.emit("stage_started", stage="ocr")
    emitter.emit("artifact", kind="lrc", path=Path("歌词.lrc"))
    emitter.emit("completed", result={"status": "complete"})

    records = _decode_stream(stream.getvalue())
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert {record["job_id"] for record in records} == {"job-中文"}
    assert all(record["event"] == record["type"] for record in records)
    assert records[1]["path"] == "歌词.lrc"
    assert emitter.terminal_event == "completed"


def test_event_emitter_generates_job_id_when_omitted() -> None:
    stream = io.StringIO()
    events.EventEmitter(stream).emit("stage_started", stage="prepare")
    [record] = _decode_stream(stream.getvalue())
    assert isinstance(record["job_id"], str) and record["job_id"]


def test_cancellation_token_supports_file_and_callable(tmp_path: Path) -> None:
    marker = tmp_path / "cancel.marker"
    token = events.CancellationToken(marker)
    events.check_cancellation(token)
    marker.write_text("cancel", encoding="ascii")

    with pytest.raises(events.PipelineCancelled):
        events.check_cancellation(token)
    with pytest.raises(events.PipelineCancelled):
        events.check_cancellation(lambda: True)


def test_preexisting_cancel_file_marks_manifest_and_releases_lock(tmp_path: Path) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    marker = tmp_path / "cancel.marker"
    marker.write_text("cancel", encoding="ascii")
    collected: list[dict[str, Any]] = []
    work_root = tmp_path / "cache"

    with pytest.raises(events.PipelineCancelled):
        pipeline.run_pipeline(
            pipeline.PipelineOptions(
                video_path=video,
                output_path=tmp_path / "song.lrc",
                work_root=work_root,
            ),
            events=collected,
            cancellation=events.CancellationToken(marker),
        )

    [manifest_path] = list(work_root.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert manifest["cancellation"]["message"] == "Cancellation requested"
    assert not (manifest_path.parent / ".run.lock").exists()
    assert [record["type"] for record in collected] == ["cancelled"]


def test_pipeline_emits_failed_even_before_manifest_exists(tmp_path: Path) -> None:
    collected: list[dict[str, Any]] = []

    with pytest.raises(FileNotFoundError):
        pipeline.run_pipeline(
            pipeline.PipelineOptions(video_path=tmp_path / "missing.mp4"),
            events=collected,
        )

    assert [record["type"] for record in collected] == ["failed"]
    assert collected[0]["error"]["type"] == "FileNotFoundError"


def test_pipeline_emits_stages_progress_artifacts_and_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_successful_pipeline(monkeypatch)
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    stream = io.StringIO()
    emitter = events.EventEmitter(stream, job_id="job-42")

    result = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=tmp_path / "song.lrc",
            work_root=tmp_path / "cache",
            keep_workdir=True,
        ),
        events=emitter,
    )

    records = _decode_stream(stream.getvalue())
    starts = [record["stage"] for record in records if record["type"] == "stage_started"]
    completes = [
        record["stage"] for record in records if record["type"] == "stage_completed"
    ]
    assert starts == ["prepare", "extract", "ocr", "detector", "writer"]
    assert completes == starts
    progress = [record for record in records if record["type"] == "progress"]
    assert [(item["current"], item["total"], item["ratio"]) for item in progress] == [
        (1, 2, 0.5),
        (2, 2, 1.0),
    ]
    artifact_kinds = {record["kind"] for record in records if record["type"] == "artifact"}
    assert {"lines", "lrc", "review", "manifest"} <= artifact_kinds
    assert records[-1]["type"] == "completed"
    assert records[-1]["result"]["status"] == "complete"
    assert result["status"] == "complete"
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))


def test_cancel_during_ocr_progress_has_cancelled_terminal_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "cancel.marker"
    _patch_successful_pipeline(monkeypatch, request_cancel=marker)
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    collected: list[dict[str, Any]] = []
    work_root = tmp_path / "cache"

    with pytest.raises(events.PipelineCancelled):
        pipeline.run_pipeline(
            pipeline.PipelineOptions(
                video_path=video,
                output_path=tmp_path / "song.lrc",
                work_root=work_root,
                keep_workdir=True,
            ),
            events=collected,
            cancellation=events.CancellationToken(marker),
        )

    event_types = [record["type"] for record in collected]
    assert "progress" in event_types
    assert event_types[-1] == "cancelled"
    assert "failed" not in event_types
    [manifest_path] = list(work_root.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert not (manifest_path.parent / ".run.lock").exists()
    assert not (tmp_path / "song.lrc").exists()


def test_main_event_stream_is_protocol_only_and_passes_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = tmp_path / "cancel.marker"
    freeze_calls: list[bool] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(main.multiprocessing, "freeze_support", lambda: freeze_calls.append(True))

    def fake_run(_options: pipeline.PipelineOptions, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        kwargs["events"].emit("completed", result={"status": "complete"})
        return {"status": "complete", "line_count": 0, "run_dir": "work/run"}

    monkeypatch.setattr(main, "run_pipeline", fake_run)
    code = main.main(
        [
            "song.mp4",
            "--event-stream",
            "--json",
            "--job-id",
            "gui-job",
            "--cancel-file",
            str(marker),
        ]
    )

    assert code == 0
    records = _decode_stream(capsys.readouterr().out)
    assert [record["type"] for record in records] == ["completed"]
    assert records[0]["job_id"] == "gui-job"
    assert isinstance(captured["cancellation"], events.CancellationToken)
    assert freeze_calls == [True]


def test_main_emits_cancelled_when_pipeline_cancels_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        main,
        "run_pipeline",
        lambda _options, **_kwargs: (_ for _ in ()).throw(
            events.PipelineCancelled("Cancellation requested")
        ),
    )

    assert main.main(["song.mp4", "--event-stream", "--job-id", "job-cancel"]) == 130
    records = _decode_stream(capsys.readouterr().out)
    assert [record["type"] for record in records] == ["cancelled"]
    assert records[0]["job_id"] == "job-cancel"


def test_late_cancellation_after_lrc_commit_reports_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_successful_pipeline(monkeypatch)
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "song.lrc"
    marker = tmp_path / "cancel.marker"
    original_write_lrc = pipeline.write_lrc

    def write_then_cancel(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_write_lrc(*args, **kwargs)
        marker.write_text("cancel", encoding="ascii")
        return result

    monkeypatch.setattr(pipeline, "write_lrc", write_then_cancel)
    collected: list[dict[str, Any]] = []
    result = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=output,
            work_root=tmp_path / "cache",
            keep_workdir=True,
        ),
        events=collected,
        cancellation=events.CancellationToken(marker),
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert manifest["status"] == "complete"
    assert output.read_text(encoding="utf-8") == "[00:00.00]歌词\n"
    assert collected[-1]["type"] == "completed"
    assert "cancelled" not in [record["type"] for record in collected]


def test_failing_event_sink_does_not_change_pipeline_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_successful_pipeline(monkeypatch)
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")

    class BrokenSink:
        def __init__(self) -> None:
            self.calls = 0

        def emit(self, _event_type: str, **_fields: Any) -> None:
            self.calls += 1
            raise BrokenPipeError("consumer closed")

    sink = BrokenSink()
    result = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=tmp_path / "song.lrc",
            work_root=tmp_path / "cache",
            keep_workdir=True,
        ),
        events=sink,
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert manifest["status"] == "complete"
    assert sink.calls == 1


@pytest.mark.parametrize("outcome, expected_code", [("success", 0), ("failure", 2)])
def test_main_preserves_exit_code_when_event_stream_breaks(
    outcome: str,
    expected_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStream:
        def write(self, _value: str) -> int:
            raise BrokenPipeError("consumer closed")

        def flush(self) -> None:
            pass

    monkeypatch.setattr(
        main,
        "EventEmitter",
        lambda *, job_id=None: events.EventEmitter(BrokenStream(), job_id=job_id),
    )

    def fake_run(_options: pipeline.PipelineOptions, **kwargs: Any) -> dict[str, Any]:
        if outcome == "failure":
            raise pipeline.PipelineError("expected failure")
        kwargs["events"].emit("completed", result={"status": "complete"})
        return {"status": "complete", "line_count": 0, "run_dir": "work/run"}

    monkeypatch.setattr(main, "run_pipeline", fake_run)
    assert main.main(["song.mp4", "--event-stream"]) == expected_code
