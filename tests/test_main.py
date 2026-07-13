from __future__ import annotations

from pathlib import Path

import pytest

import main


def test_main_passes_explicit_roi_without_default_crop(
    monkeypatch,
    capsys,
) -> None:
    captured = {}

    def fake_run(options):
        captured["options"] = options
        return {
            "status": "preview_complete",
            "preview_path": "preview.jpg",
            "run_dir": "work/run",
        }

    monkeypatch.setattr(main, "run_pipeline", fake_run)
    code = main.main(
        [
            "song.mp4",
            "--roi",
            "0.1,0.6,0.8,0.3",
            "--preview-roi",
            "--json",
        ]
    )

    assert code == 0
    assert captured["options"].roi == (0.1, 0.6, 0.8, 0.3)
    assert captured["options"].crop_bottom_ratio is None
    assert '"status": "preview_complete"' in capsys.readouterr().out


def test_main_uses_default_bottom_crop(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        main,
        "run_pipeline",
        lambda options: captured.setdefault("options", options)
        or {"status": "dry_run_complete", "run_dir": "work/run"},
    )

    # Use a full function because setdefault returns the options object.
    def fake_run(options):
        captured["options"] = options
        return {"status": "dry_run_complete", "run_dir": "work/run", "line_count": 0}

    monkeypatch.setattr(main, "run_pipeline", fake_run)
    assert main.main(["song.mp4", "--dry-run"]) == 0
    assert captured["options"].crop_bottom_ratio == main.DEFAULT_CROP_BOTTOM_RATIO


def test_main_reports_pipeline_error(monkeypatch, capsys) -> None:
    def fail(_options):
        raise main.PipelineError("ffprobe is missing")

    monkeypatch.setattr(main, "run_pipeline", fail)
    assert main.main([str(Path("missing.mp4"))]) == 2
    assert "ffprobe is missing" in capsys.readouterr().err


def test_main_reports_os_error_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        main,
        "run_pipeline",
        lambda _options: (_ for _ in ()).throw(PermissionError("access denied")),
    )
    assert main.main(["song.mp4"]) == 2
    assert "access denied" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["song.mp4", "--fps", "nan"],
        ["song.mp4", "--fps", "inf"],
        ["song.mp4", "--roi", "nan,0.5,0.5,0.5"],
        ["song.mp4", "--min-line-gap-ms", "-1"],
    ],
)
def test_parser_rejects_non_finite_or_negative_numbers(args) -> None:
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(args)
