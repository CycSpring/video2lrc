from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import pipeline


def test_frozen_default_work_root_uses_local_app_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert pipeline._default_work_root(frozen=True) == tmp_path / "Video2LRC" / "work"
    assert pipeline._default_work_root(frozen=False) == pipeline.PROJECT_ROOT / "work"


def test_safe_cleanup_frames_supports_custom_work_root(tmp_path: Path) -> None:
    work_root = tmp_path / "custom-cache"
    run_dir = work_root / "run-1"
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / "000001.png").write_bytes(b"png")

    assert pipeline._safe_cleanup_frames(run_dir, work_root, frames_dir) is True
    assert not frames_dir.exists()

    outside_run = tmp_path / "outside"
    outside_frames = outside_run / "frames"
    outside_frames.mkdir(parents=True)
    with pytest.raises(pipeline.PipelineError, match="outside the work root"):
        pipeline._safe_cleanup_frames(outside_run, work_root, outside_frames)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_resume_and_cleanup_reject_windows_junction(tmp_path: Path) -> None:
    work_root = tmp_path / "cache"
    target = work_root / "valuable-target"
    target_frames = target / "frames"
    target_frames.mkdir(parents=True)
    valuable = target_frames / "valuable.txt"
    valuable.write_text("keep", encoding="utf-8")
    junction = work_root / "abc123"

    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr}")

    try:
        assert pipeline._is_link_or_reparse(junction) is True
        with pytest.raises(pipeline.PipelineError, match="linked run cache"):
            pipeline._reserve_run_dir(work_root, "abc123", resume=True)
        with pytest.raises(pipeline.PipelineError, match="linked run or frames"):
            pipeline._safe_cleanup_frames(junction, work_root, junction / "frames")
        assert valuable.read_text(encoding="utf-8") == "keep"
    finally:
        os.rmdir(junction)
    assert valuable.read_text(encoding="utf-8") == "keep"


def test_fingerprint_file_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    video = tmp_path / "视频 sample.mp4"
    video.write_bytes(b"first-content")

    first = pipeline.fingerprint_file(video)
    second = pipeline.fingerprint_file(video)
    assert first == second

    video.write_bytes(b"second-content")
    changed = pipeline.fingerprint_file(video)
    assert changed["sha256"] != first["sha256"]


def test_fingerprint_detects_middle_change_with_preserved_size_and_mtime(tmp_path: Path) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"a" * 1024 + b"middle-A" + b"z" * 1024)
    original_stat = video.stat()
    first = pipeline.fingerprint_file(video, chunk_size=127)

    video.write_bytes(b"a" * 1024 + b"middle-B" + b"z" * 1024)
    os.utime(video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    changed = pipeline.fingerprint_file(video, chunk_size=127)

    assert changed["size"] == first["size"]
    assert changed["mtime_ns"] == first["mtime_ns"]
    assert changed["sha256"] != first["sha256"]


def test_pipeline_reuses_detector_when_only_offset_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"fake video")
    output = tmp_path / "song.lrc"
    work_root = tmp_path / "cache"
    calls = {"extract": 0, "ocr": 0, "detect": 0, "write": 0}
    seen_ocr_options = {}

    monkeypatch.setattr(
        pipeline,
        "ensure_media_tools",
        lambda **_: {"ffmpeg": "test", "ffprobe": "test"},
    )
    monkeypatch.setattr(
        pipeline,
        "probe_video",
        lambda *_args, **_kwargs: {
            "width": 1920,
            "height": 1080,
            "rotation": 0,
            "start_time": 0.0,
            "duration": 1.0,
        },
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_roi",
        lambda *_args, **_kwargs: {"x": 0, "y": 648, "width": 1920, "height": 432},
    )

    def fake_extract(_video, frames_dir, roi=None, fps=None, **_kwargs):
        calls["extract"] += 1
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame = frames_dir / "000001.png"
        frame.write_bytes(b"png")
        return {"frame_index": [{"frame": frame.name, "path": str(frame), "time": 0.0}]}

    def fake_ocr(_frames, **_kwargs):
        calls["ocr"] += 1
        seen_ocr_options.update(_kwargs)
        return [{"frame": "000001.png", "time": 0.0, "text": "第一句", "confidence": 0.95}]

    def fake_detect(_frames, **_kwargs):
        calls["detect"] += 1
        return {
            "style": "line",
            "lines": [
                {
                    "start_time_raw": 0.0,
                    "end_time": 0.5,
                    "text": "第一句",
                    "confidence": 0.95,
                    "support_frames": 2,
                    "qa_flags": [],
                }
            ],
        }

    def fake_write(result, output_path, **kwargs):
        calls["write"] += 1
        output_path = Path(output_path)
        output_path.write_text("[00:00.00]第一句\n", encoding="utf-8")
        review_path = Path(kwargs["review_path"])
        review_path.write_text("line_no,text\n1,第一句\n", encoding="utf-8-sig")
        return {"line_count": len(result["lines"]), "preview": ["[00:00.00]第一句"]}

    monkeypatch.setattr(
        pipeline,
        "extract_frames",
        create_autospec(pipeline.extract_frames, side_effect=fake_extract),
    )
    monkeypatch.setattr(pipeline, "ocr_all_frames", fake_ocr)
    monkeypatch.setattr(pipeline, "detect_line_switches", fake_detect)
    monkeypatch.setattr(pipeline, "write_lrc", fake_write)

    first = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=output,
            work_root=work_root,
            keep_workdir=True,
        )
    )
    assert first["status"] == "complete"
    assert calls == {"extract": 1, "ocr": 1, "detect": 1, "write": 1}
    assert seen_ocr_options["engine_kwargs"] == {"Global.text_score": 0.1}

    second = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=output,
            work_root=work_root,
            keep_workdir=True,
            resume=True,
            offset_ms=250,
            force=True,
        )
    )
    assert second["status"] == "complete"
    assert calls == {"extract": 1, "ocr": 1, "detect": 1, "write": 2}

    manifest = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["stages"]["detector"]["reused"] is True
    assert manifest["status"] == "complete"

    run_dir = Path(second["run_dir"])
    for name in ("ocr_raw.json", "ocr_clean.json", "lines.json"):
        (run_dir / name).unlink()
    (run_dir / "frames" / "000001.png").unlink()
    third = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=output,
            work_root=work_root,
            keep_workdir=True,
            resume=True,
            offset_ms=500,
            force=True,
        )
    )
    assert third["status"] == "complete"
    assert calls == {"extract": 2, "ocr": 2, "detect": 2, "write": 3}


def test_pipeline_rejects_roi_with_crop_ratio(tmp_path: Path) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"fake")
    with pytest.raises(ValueError, match="mutually exclusive"):
        pipeline.run_pipeline(
            pipeline.PipelineOptions(
                video_path=video,
                roi=(0.0, 0.5, 1.0, 0.5),
                crop_bottom_ratio=0.4,
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fps", float("nan")),
        ("confidence_threshold", float("inf")),
        ("same_threshold", float("nan")),
        ("min_line_gap_ms", -1),
    ],
)
def test_pipeline_rejects_invalid_numeric_options(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"fake")
    kwargs = {field: value}
    with pytest.raises(ValueError):
        pipeline.run_pipeline(pipeline.PipelineOptions(video_path=video, **kwargs))


def test_pipeline_never_allows_output_to_replace_input(tmp_path: Path) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"valuable video")
    with pytest.raises(ValueError, match="input video"):
        pipeline.run_pipeline(
            pipeline.PipelineOptions(video_path=video, output_path=video, force=True)
        )
    assert video.read_bytes() == b"valuable video"


def test_pipeline_rejects_output_anywhere_inside_work_root(tmp_path: Path) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    work_root = tmp_path / "cache"
    output = work_root / "another-run" / "lines.json"
    with pytest.raises(ValueError, match="work cache"):
        pipeline.run_pipeline(
            pipeline.PipelineOptions(
                video_path=video,
                output_path=output,
                work_root=work_root,
                force=True,
            )
        )


def test_preview_uses_the_real_extractor_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(pipeline, "ensure_media_tools", lambda **_: {"ffmpeg": {}, "ffprobe": {}})
    monkeypatch.setattr(
        pipeline,
        "probe_video",
        lambda *_args, **_kwargs: {
            "width": 640,
            "height": 360,
            "display_width": 640,
            "display_height": 360,
            "rotation": 0,
            "start_time": 0.0,
            "duration": 4.0,
        },
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_roi",
        lambda *_args, **_kwargs: {"x": 0, "y": 216, "width": 640, "height": 144},
    )

    def fake_preview(_video, output_path, **_kwargs):
        Path(output_path).write_bytes(b"jpeg")
        return {"sample_count": 3}

    preview_mock = create_autospec(
        pipeline.create_roi_preview,
        side_effect=fake_preview,
    )
    monkeypatch.setattr(pipeline, "create_roi_preview", preview_mock)
    result = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            work_root=tmp_path / "cache",
            preview_roi=True,
        )
    )

    assert result["status"] == "preview_complete"
    assert Path(result["preview_path"]).read_bytes() == b"jpeg"
    preview_mock.assert_called_once()


def test_all_ocr_failures_mark_run_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    work_root = tmp_path / "cache"
    monkeypatch.setattr(pipeline, "ensure_media_tools", lambda **_: {"ffmpeg": {}, "ffprobe": {}})
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
        lambda *_args, **_kwargs: {"x": 0, "y": 216, "width": 640, "height": 144},
    )

    def fake_extract(_video, frames_dir, **_kwargs):
        frame = Path(frames_dir) / "000001.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"png")
        return {"frame_index": [{"frame": frame.name, "path": str(frame), "time": 0.0}]}

    monkeypatch.setattr(
        pipeline,
        "extract_frames",
        create_autospec(pipeline.extract_frames, side_effect=fake_extract),
    )
    monkeypatch.setattr(
        pipeline,
        "ocr_all_frames",
        lambda *_args, **_kwargs: [
            {"frame": "000001.png", "time": 0.0, "text": "", "status": "error", "error": {"message": "boom"}}
        ],
    )

    with pytest.raises(pipeline.PipelineError, match="every frame"):
        pipeline.run_pipeline(
            pipeline.PipelineOptions(
                video_path=video,
                output_path=tmp_path / "song.lrc",
                work_root=work_root,
                keep_workdir=True,
            )
        )

    manifests = list(work_root.glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"]["type"] == "PipelineError"
    assert "ocr" not in manifest["stages"]
    assert not (manifests[0].parent / ".run.lock").exists()

    monkeypatch.setattr(
        pipeline,
        "ocr_all_frames",
        lambda *_args, **_kwargs: [
            {
                "frame": "000001.png",
                "time": 0.0,
                "text": "歌词",
                "status": "ok",
                "confidence": 0.99,
            }
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "detect_line_switches",
        lambda *_args, **_kwargs: {
            "lines": [
                {
                    "start_time_raw": 0.0,
                    "end_time": 1.0,
                    "text": "歌词",
                    "confidence": 0.99,
                    "support_frames": 2,
                    "qa_flags": [],
                }
            ]
        },
    )
    recovered = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=tmp_path / "song.lrc",
            work_root=work_root,
            keep_workdir=True,
            resume=True,
        )
    )
    assert recovered["status"] == "complete"


def test_resume_clears_stale_terminal_manifest_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "song.lrc"
    work_root = tmp_path / "cache"
    fingerprint = pipeline.fingerprint_file(video)
    run_dir = work_root / fingerprint["sha256"][:16]
    run_dir.mkdir(parents=True)
    lines_path = run_dir / "lines.json"
    lines_path.write_text(
        json.dumps(
            {
                "lines": [
                    {
                        "start_time_raw": 0.0,
                        "end_time": 1.0,
                        "text": "歌词",
                        "confidence": 0.9,
                        "support_frames": 2,
                        "qa_flags": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "data_format_version": pipeline.DATA_FORMAT_VERSION,
                "status": "failed",
                "attempt": 1,
                "error": {"type": "RuntimeError", "message": "old"},
                "output": "old.lrc",
                "frames_cleaned": False,
                "stages": {"detector": {"key": "same", "details": {"line_count": 1}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "_stable_hash", lambda _value: "same")
    monkeypatch.setattr(pipeline, "ensure_media_tools", lambda **_: {"ffmpeg": {}, "ffprobe": {}})
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
        lambda *_args, **_kwargs: {"x": 0, "y": 216, "width": 640, "height": 144},
    )

    result = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=output,
            work_root=work_root,
            resume=True,
        )
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["attempt"] == 2
    assert "error" not in manifest
    assert manifest["output"] == str(output.resolve())
    assert manifest["stages"]["detector"]["details"] == {"line_count": 1}

    new_output = tmp_path / "new-song.lrc"
    second = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=new_output,
            work_root=work_root,
            resume=True,
        )
    )
    assert second["status"] == "complete"
    assert new_output.read_text(encoding="utf-8") == "[00:00.00]歌词\n"

    dry_run = pipeline.run_pipeline(
        pipeline.PipelineOptions(
            video_path=video,
            output_path=tmp_path / "dry.lrc",
            work_root=work_root,
            resume=True,
            dry_run=True,
        )
    )
    dry_manifest = json.loads(Path(dry_run["manifest_path"]).read_text(encoding="utf-8"))
    assert dry_run["status"] == "dry_run_complete"
    assert dry_run["review_path"] is None
    assert dry_manifest["stages"]["writer"]["artifacts"] == []
    assert "output" not in dry_manifest
    assert dry_manifest["would_write_output"].endswith("dry.lrc")


def test_stage_invalidation_prevents_old_key_from_pointing_to_new_artifact(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    lines_path = tmp_path / "lines.json"
    manifest = {
        "data_format_version": pipeline.DATA_FORMAT_VERSION,
        "stages": {
            "detector": {"key": "old-key"},
            "writer": {"key": "old-writer"},
        },
    }
    pipeline.atomic_write_json(manifest_path, manifest)

    pipeline._invalidate_stages(manifest, manifest_path, "detector", "writer")
    pipeline.atomic_write_json(lines_path, {"lines": [{"text": "new artifact"}]})

    reloaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert not pipeline._cache_hit(reloaded, "detector", "old-key", [lines_path])
    assert "detector" not in reloaded["stages"]
    assert "writer" not in reloaded["stages"]


def test_run_lock_rejects_concurrent_use_and_is_releasable(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock = pipeline._acquire_run_lock(run_dir)
    with pytest.raises(pipeline.PipelineError, match="already locked"):
        pipeline._acquire_run_lock(run_dir)
    pipeline._release_run_lock(lock)

    second = pipeline._acquire_run_lock(run_dir)
    pipeline._release_run_lock(second)
    assert not second.exists()


def test_non_resume_run_directories_are_reserved_atomically(tmp_path: Path) -> None:
    first = pipeline._reserve_run_dir(tmp_path, "abc123", resume=False)
    second = pipeline._reserve_run_dir(tmp_path, "abc123", resume=False)
    assert first != second
    assert first.is_dir() and second.is_dir()
