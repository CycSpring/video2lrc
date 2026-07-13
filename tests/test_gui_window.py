from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from video2lrc_ui.main_window import MainWindow
from video2lrc_ui.app import application_icon_path
from video2lrc_ui.models import ROI
from video2lrc_ui.process_runner import RunnerState
from video2lrc_ui.theme import apply_theme


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    instance.setApplicationName("Video2LRC-Test")
    apply_theme(instance)
    return instance


def _settings(tmp_path: Path, name: str) -> QSettings:
    return QSettings(str(tmp_path / f"{name}.ini"), QSettings.Format.IniFormat)


def test_application_icon_is_loadable(app: QApplication) -> None:
    path = application_icon_path()

    assert app is QApplication.instance()
    assert path.is_file()
    assert not QIcon(str(path)).isNull()


def test_default_workdir_matches_frozen_cli_root(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = MainWindow(settings=_settings(tmp_path, "workdir"))

    assert window._default_workdir() == tmp_path / "Video2LRC" / "work"
    window.close()


def test_window_builds_stable_desktop_layout(
    app: QApplication,
    tmp_path: Path,
) -> None:
    window = MainWindow(settings=_settings(tmp_path, "layout"))
    window.resize(1180, 760)
    window.show()
    app.processEvents()

    assert window.minimumWidth() == 720
    assert window.minimumHeight() == 600
    assert window.main_splitter.count() == 2
    assert window.result_tabs.count() == 2
    assert window.log_view.maximumBlockCount() == 2000
    assert window.run_button.height() >= 38
    assert window.cancel_button.isEnabled() is False
    window.close()


def test_window_maps_bottom_and_custom_roi_to_job_spec(
    app: QApplication,
    tmp_path: Path,
) -> None:
    video = tmp_path / "示例 & 视频.mp4"
    video.write_bytes(b"video")
    window = MainWindow(video, settings=_settings(tmp_path, "roi"))

    bottom = window._build_job_spec()
    assert bottom.crop_bottom_ratio == pytest.approx(0.4)
    assert bottom.roi is None

    window.roi_mode.setCurrentIndex(window.roi_mode.findData("custom"))
    app.processEvents()
    custom = window._build_job_spec()
    assert isinstance(custom.roi, ROI)
    assert custom.roi.as_tuple() == pytest.approx((0.18, 0.76, 0.78, 0.22))
    assert custom.crop_bottom_ratio is None
    window.close()


def test_existing_output_requires_explicit_overwrite(
    app: QApplication,
    tmp_path: Path,
) -> None:
    video = tmp_path / "song.mp4"
    output = tmp_path / "song.lrc"
    video.write_bytes(b"video")
    output.write_text("[00:00.00]歌词\n", encoding="utf-8")
    window = MainWindow(video, settings=_settings(tmp_path, "overwrite"))
    app.processEvents()

    assert window.run_button.isEnabled() is False
    assert window.banner.status() == "warning"

    window.force_check.setChecked(True)
    app.processEvents()
    assert window.run_button.isEnabled() is True
    window.close()


def test_start_preview_passes_validated_spec_without_shell(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "video with spaces & symbols.mp4"
    video.write_bytes(b"video")
    window = MainWindow(video, settings=_settings(tmp_path, "preview"))
    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        window.runner,
        "start",
        lambda spec, preview=False: calls.append((spec, preview)) or "job-id",
    )

    window._start_preview()

    assert len(calls) == 1
    spec, preview = calls[0]
    assert preview is True
    assert str(spec.video_path) == str(video)
    window.close()


def test_changing_video_preserves_custom_output(
    app: QApplication,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    custom = tmp_path / "custom-name.lrc"
    window = MainWindow(first, settings=_settings(tmp_path, "custom-output"))
    window.output_field.set_path(custom)

    window.set_video_path(second)
    app.processEvents()

    assert window.output_field.path() == str(custom)
    window.close()


def test_reset_parameters_requires_confirmation_and_preserves_paths(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "keep-video.mp4"
    output = tmp_path / "keep-output.lrc"
    video.write_bytes(b"video")
    window = MainWindow(video, settings=_settings(tmp_path, "reset-parameters"))
    window.output_field.set_path(output)

    window.roi_mode.setCurrentIndex(window.roi_mode.findData("custom"))
    window.bottom_ratio.setValue(70)
    window.active_row.setCurrentIndex(window.active_row.findData("bottom"))
    for key, value in {"x": 5.0, "y": 60.0, "width": 80.0, "height": 30.0}.items():
        window.roi_spins[key].setValue(value)
    window.fps.setValue(10.0)
    window.workers.setValue(window.workers.maximum())
    window.style_combo.setCurrentIndex(window.style_combo.findData("typewriter"))
    window.offset_ms.setValue(5_000)
    window.confidence.setValue(75)
    window.same_threshold.setValue(70)
    window.confirm_frames.setValue(5)
    window.min_gap_ms.setValue(2_000)
    window.max_blank_frames.setValue(8)
    window.resume_check.setChecked(False)
    window.keep_workdir_check.setChecked(True)
    window.force_check.setChecked(True)
    window.strict_check.setChecked(True)
    window.advanced.set_expanded(True)

    responses = iter(
        (QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: next(responses),
    )

    window.reset_parameters_button.click()
    assert window.fps.value() == 10.0

    window.reset_parameters_button.click()
    app.processEvents()

    assert window.video_field.path() == str(video)
    assert window.output_field.path() == str(output)
    assert window.roi_mode.currentData() == "bottom"
    assert window.bottom_ratio.value() == 40
    assert window.active_row.currentData() == "auto"
    assert {key: spin.value() for key, spin in window.roi_spins.items()} == {
        "x": 18.0,
        "y": 76.0,
        "width": 78.0,
        "height": 22.0,
    }
    assert window.fps.value() == 4.0
    assert window.workers.value() == min(4, window.workers.maximum())
    assert window.style_combo.currentData() == "line"
    assert window.offset_ms.value() == 0
    assert window.confidence.value() == 50
    assert window.same_threshold.value() == 90
    assert window.confirm_frames.value() == 2
    assert window.min_gap_ms.value() == 800
    assert window.max_blank_frames.value() == 2
    assert window.resume_check.isChecked() is True
    assert window.keep_workdir_check.isChecked() is False
    assert window.force_check.isChecked() is False
    assert window.strict_check.isChecked() is False
    assert window.advanced.is_expanded() is False
    assert "参数已恢复" in window.statusBar().currentMessage()
    window.close()


def test_running_job_disables_and_rejects_drag_and_drop(
    app: QApplication,
    tmp_path: Path,
) -> None:
    video = tmp_path / "running.mp4"
    video.write_bytes(b"video")
    window = MainWindow(video, settings=_settings(tmp_path, "active-drop"))

    class IgnoredEvent:
        ignored = False

        def ignore(self) -> None:
            self.ignored = True

        def mimeData(self):  # noqa: N802
            raise AssertionError("active-state handlers must not inspect dropped data")

    window.runner._set_state(RunnerState.RUNNING)
    app.processEvents()
    original_path = window.video_field.path()

    drag_event = IgnoredEvent()
    drop_event = IgnoredEvent()
    window.dragEnterEvent(drag_event)
    window.dropEvent(drop_event)

    assert window.acceptDrops() is False
    assert window.reset_parameters_button.isEnabled() is False
    assert drag_event.ignored is True
    assert drop_event.ignored is True
    assert window.video_field.path() == original_path

    window.runner._set_state(RunnerState.SUCCEEDED)
    app.processEvents()
    assert window.acceptDrops() is True
    assert window.reset_parameters_button.isEnabled() is True
    window.close()


def test_close_rechecks_runner_after_confirmation_dialog(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(settings=_settings(tmp_path, "close-race"))
    window.show()
    window.runner._set_state(RunnerState.RUNNING)

    def finish_while_confirming(*_args, **_kwargs):
        window.runner._set_state(RunnerState.SUCCEEDED)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", finish_while_confirming)
    close_event = QCloseEvent()

    window.closeEvent(close_event)

    assert close_event.isAccepted() is True
    assert window._close_after_finish is False
    window.close()


def test_corrupt_settings_fall_back_to_safe_defaults(
    app: QApplication,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "corrupt")
    invalid_values = {
        "window/geometry": "not-a-qbytearray",
        "window/main_splitter": "not-a-qbytearray",
        "window/result_splitter": "not-a-qbytearray",
        "options/bottom_ratio": "many",
        "options/fps": "nan",
        "options/workers": "999999999999999999999",
        "options/offset_ms": "999999",
        "options/resume": "sometimes",
        "options/advanced": "perhaps",
    }
    for key, value in invalid_values.items():
        settings.setValue(key, value)

    window = MainWindow(settings=settings)

    assert window.bottom_ratio.value() == 40
    assert window.fps.value() == 4.0
    assert window.workers.value() == min(4, window.workers.maximum())
    assert window.offset_ms.value() == 0
    assert window.resume_check.isChecked() is True
    assert window.advanced.is_expanded() is False
    assert all(not settings.contains(key) for key in invalid_values)
    window.close()
