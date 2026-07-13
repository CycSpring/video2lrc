from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QLabel

from video2lrc_ui.theme import APP_STYLESHEET, apply_theme
from video2lrc_ui.widgets import (
    AspectPreviewLabel,
    CollapsibleSection,
    PathField,
    StatusBanner,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    apply_theme(instance)
    return instance


def test_theme_is_lightweight_and_uses_small_corner_radii(app: QApplication) -> None:
    assert app.styleSheet() == APP_STYLESHEET
    assert "gradient" not in APP_STYLESHEET.lower()
    assert "shadow" not in APP_STYLESHEET.lower()
    radii = [int(value) for value in re.findall(r"border-radius:\s*(\d+)px", APP_STYLESHEET)]
    assert radii and max(radii) <= 6


def test_path_field_retains_and_elides_a_long_path(app: QApplication) -> None:
    field = PathField(placeholder="选择视频")
    field.resize(250, 34)
    field.show()
    path = r"D:\very-long-folder-name\another-long-folder\video-with-a-long-name.mp4"
    changes: list[str] = []
    field.pathChanged.connect(changes.append)

    field.set_path(path)
    field.browse_button.setFocus()
    app.processEvents()

    assert field.path() == path
    assert field.line_edit.toolTip() == path
    assert changes == [path]
    assert "…" in field.line_edit.displayed_text()
    assert field.browse_button.icon().isNull() is False
    assert field.browse_button.width() == field.browse_button.height() == 34


def test_path_field_browse_emits_signals_and_updates_value(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = PathField(dialog_filter="视频 (*.mp4)")
    requested: list[bool] = []
    changed: list[str] = []
    field.browseRequested.connect(lambda: requested.append(True))
    field.pathChanged.connect(changed.append)
    monkeypatch.setattr(
        "video2lrc_ui.widgets.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: ("D:/media/demo.mp4", "视频 (*.mp4)"),
    )

    field.browse_button.click()
    app.processEvents()

    assert requested == [True]
    assert field.path() == "D:/media/demo.mp4"
    assert changed == ["D:/media/demo.mp4"]


def test_path_field_uses_save_dialog_for_output(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = PathField(save_file=True, dialog_filter="LRC (*.lrc)")
    monkeypatch.setattr(
        "video2lrc_ui.widgets.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: ("D:/media/result.lrc", "LRC (*.lrc)"),
    )

    field.browse_button.click()
    app.processEvents()

    assert field.path() == "D:/media/result.lrc"


def test_path_field_rejects_conflicting_dialog_modes(app: QApplication) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        PathField(select_directory=True, save_file=True)


def test_preview_has_empty_state_and_preserves_pixmap_ratio(app: QApplication) -> None:
    preview = AspectPreviewLabel("等待预览")
    preview.resize(220, 180)
    preview.show()
    assert preview.text() == "等待预览"

    source = QPixmap(400, 200)
    source.fill(QColor("#0F766E"))
    preview.setPixmap(source)
    app.processEvents()
    rendered = preview.pixmap()

    assert rendered is not None and not rendered.isNull()
    assert rendered.width() / rendered.height() == pytest.approx(2.0, rel=0.02)
    assert rendered.width() <= preview.contentsRect().width()
    assert rendered.height() <= preview.contentsRect().height()
    assert preview.source_pixmap().size() == source.size()

    preview.clear_preview()
    assert preview.text() == "等待预览"
    assert preview.source_pixmap().isNull()


def test_collapsible_section_exposes_content_layout_and_toggles(app: QApplication) -> None:
    section = CollapsibleSection("高级设置")
    section.content_layout().addWidget(QLabel("选项"))
    states: list[bool] = []
    section.toggled.connect(states.append)

    assert section.is_expanded() is False
    assert section.content_widget.isHidden()

    section.set_expanded(True)
    app.processEvents()
    assert section.is_expanded() is True
    assert not section.content_widget.isHidden()
    assert states == [True]


@pytest.mark.parametrize("status", ["info", "warning", "error", "success"])
def test_status_banner_exposes_semantic_status(app: QApplication, status: str) -> None:
    banner = StatusBanner("处理中", status)

    assert banner.text() == "处理中"
    assert banner.status() == status
    assert banner.property("status") == status
    assert banner.toolTip() == "处理中"


def test_status_banner_rejects_unknown_status(app: QApplication) -> None:
    banner = StatusBanner()
    with pytest.raises(ValueError, match="Unsupported status"):
        banner.set_status("未知", "pending")
