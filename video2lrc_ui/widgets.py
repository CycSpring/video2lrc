"""Reusable, dependency-free Qt Widgets used by the desktop interface."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QFocusEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class _ElidingPathEdit(QLineEdit):
    """Show a middle-elided path while retaining its complete editable value."""

    fullTextChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.textEdited.connect(self._remember_edit)

    def text(self) -> str:  # noqa: N802 - mirrors QLineEdit's Qt API
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLineEdit's Qt API
        value = str(text)
        if value == self._full_text:
            self._sync_display()
            return
        self._full_text = value
        self.setToolTip(value)
        self._sync_display()
        self.fullTextChanged.emit(value)

    def clear(self) -> None:
        self.setText("")

    def displayed_text(self) -> str:
        """Return the currently painted value, which may contain an ellipsis."""

        return super().text()

    def _remember_edit(self, value: str) -> None:
        self._full_text = value
        self.setToolTip(value)
        self.fullTextChanged.emit(value)

    def _sync_display(self) -> None:
        if self.hasFocus():
            display = self._full_text
        else:
            margins = self.textMargins()
            width = max(0, self.contentsRect().width() - margins.left() - margins.right() - 8)
            display = self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideMiddle,
                width,
            )
        blocker = QSignalBlocker(self)
        QLineEdit.setText(self, display)
        del blocker

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        blocker = QSignalBlocker(self)
        QLineEdit.setText(self, self._full_text)
        del blocker
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        value = QLineEdit.text(self)
        if value != self._full_text:
            self._full_text = value
            self.setToolTip(value)
            self.fullTextChanged.emit(value)
        super().focusOutEvent(event)
        self._sync_display()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_display()


class PathField(QWidget):
    """Editable path field with a compact native file-system browse button."""

    pathChanged = Signal(str)
    browseRequested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        placeholder: str = "",
        dialog_caption: str = "选择文件",
        dialog_filter: str = "所有文件 (*)",
        select_directory: bool = False,
        save_file: bool = False,
    ) -> None:
        super().__init__(parent)
        if select_directory and save_file:
            raise ValueError("select_directory and save_file are mutually exclusive")
        self.dialog_caption = dialog_caption
        self.dialog_filter = dialog_filter
        self.select_directory = select_directory
        self.save_file = save_file

        self.line_edit = _ElidingPathEdit(self)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setMinimumHeight(34)
        self.line_edit.setAccessibleName(placeholder or "路径")
        self.line_edit.fullTextChanged.connect(self.pathChanged)

        self.browse_button = QToolButton(self)
        self.browse_button.setObjectName("pathBrowseButton")
        self.browse_button.setFixedSize(34, 34)
        self.browse_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self.browse_button.setIconSize(QSize(18, 18))
        self.browse_button.setToolTip("浏览文件夹" if select_directory else "浏览文件")
        self.browse_button.setAccessibleName(self.browse_button.toolTip())
        self.browse_button.clicked.connect(self._browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.line_edit, 1)
        layout.addWidget(self.browse_button)

        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def path(self) -> str:
        return self.line_edit.text().strip()

    def set_path(self, path: str | Path) -> None:
        self.line_edit.setText(str(path))

    def clear(self) -> None:
        self.line_edit.clear()

    def set_enabled(self, enabled: bool) -> None:
        self.line_edit.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)

    def _initial_directory(self) -> str:
        value = self.path()
        if not value:
            return ""
        path = Path(value).expanduser()
        if path.is_dir():
            return str(path)
        return str(path.parent)

    def _browse(self, _checked: bool = False) -> None:
        self.browseRequested.emit()
        initial = self._initial_directory()
        if self.select_directory:
            selected = QFileDialog.getExistingDirectory(
                self,
                self.dialog_caption,
                initial,
            )
        elif self.save_file:
            selected, _ = QFileDialog.getSaveFileName(
                self,
                self.dialog_caption,
                self.path() or initial,
                self.dialog_filter,
            )
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self,
                self.dialog_caption,
                initial,
                self.dialog_filter,
            )
        if selected:
            self.set_path(selected)


class AspectPreviewLabel(QLabel):
    """Preview surface that scales a source pixmap without changing its ratio."""

    def __init__(
        self,
        empty_text: str = "尚未生成预览",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self._empty_text = empty_text
        self.setObjectName("previewFrame")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(240, 135)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("字幕区域预览")
        self.setText(empty_text)

    def source_pixmap(self) -> QPixmap:
        return QPixmap(self._source_pixmap)

    def setPixmap(self, pixmap: QPixmap) -> None:  # noqa: N802 - mirrors QLabel's Qt API
        if pixmap.isNull():
            self.clear_preview()
            return
        self._source_pixmap = QPixmap(pixmap)
        self.setText("")
        self._update_scaled_pixmap()

    def clear_preview(self) -> None:
        self._source_pixmap = QPixmap()
        QLabel.setPixmap(self, QPixmap())
        self.setText(self._empty_text)

    def clearPreview(self) -> None:  # noqa: N802 - convenience Qt-style alias
        self.clear_preview()

    def clear(self) -> None:
        self.clear_preview()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        if not self._source_pixmap.isNull() and self._source_pixmap.width() > 0:
            ratio = self._source_pixmap.height() / self._source_pixmap.width()
        else:
            ratio = 9 / 16
        return max(self.minimumHeight(), round(width * ratio))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, 360)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self) -> None:
        if self._source_pixmap.isNull():
            return
        available = self.contentsRect().size() - QSize(12, 12)
        if available.width() <= 0 or available.height() <= 0:
            return
        scaled = self._source_pixmap.scaled(
            available,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        QLabel.setPixmap(self, scaled)


class CollapsibleSection(QWidget):
    """A compact disclosure section intended for infrequent advanced settings."""

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.header_button = QToolButton(self)
        self.header_button.setObjectName("sectionHeader")
        self.header_button.setText(title)
        self.header_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header_button.setCheckable(True)
        self.header_button.setChecked(expanded)
        self.header_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header_button.setMinimumHeight(32)
        self.header_button.setToolTip("展开或收起高级设置")
        self.header_button.setAccessibleName(title)

        self.content_widget = QWidget(self)
        self._content_layout = QVBoxLayout(self.content_widget)
        self._content_layout.setContentsMargins(24, 6, 0, 6)
        self._content_layout.setSpacing(8)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header_button)
        layout.addWidget(self.content_widget)

        self.header_button.toggled.connect(self._on_toggled)
        self._sync_state(expanded)

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def is_expanded(self) -> bool:
        return self.header_button.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self.header_button.setChecked(bool(expanded))

    def setExpanded(self, expanded: bool) -> None:  # noqa: N802 - Qt-style alias
        self.set_expanded(expanded)

    def set_title(self, title: str) -> None:
        self.header_button.setText(title)
        self.header_button.setAccessibleName(title)

    def _on_toggled(self, expanded: bool) -> None:
        self._sync_state(expanded)
        self.toggled.emit(expanded)

    def _sync_state(self, expanded: bool) -> None:
        self.content_widget.setVisible(expanded)
        icon = QStyle.StandardPixmap.SP_ArrowDown if expanded else QStyle.StandardPixmap.SP_ArrowRight
        self.header_button.setIcon(self.style().standardIcon(icon))
        self.header_button.setToolTip("收起高级设置" if expanded else "展开高级设置")


class StatusBanner(QFrame):
    """Inline textual status message with a styleable semantic state."""

    VALID_STATUSES = frozenset({"info", "warning", "error", "success"})
    statusChanged = Signal(str)

    def __init__(
        self,
        text: str = "",
        status: str = "info",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("statusBanner")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMinimumHeight(38)

        self.label = QLabel(self)
        self.label.setObjectName("statusText")
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.addWidget(self.label, 1)

        self._status = "info"
        self.set_status(text, status)

    def text(self) -> str:
        return self.label.text()

    def status(self) -> str:
        return self._status

    def set_text(self, text: str) -> None:
        self.label.setText(text)
        self.setToolTip(text)
        self.setAccessibleName(text or "状态")

    def setText(self, text: str) -> None:  # noqa: N802 - Qt-style alias
        self.set_text(text)

    def set_status(self, text: str, status: str = "info") -> None:
        if status not in self.VALID_STATUSES:
            choices = ", ".join(sorted(self.VALID_STATUSES))
            raise ValueError(f"Unsupported status {status!r}; expected one of: {choices}")
        changed = status != self._status
        self._status = status
        self.setProperty("status", status)
        self.set_text(text)

        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()
        if changed:
            self.statusChanged.emit(status)


__all__ = [
    "AspectPreviewLabel",
    "CollapsibleSection",
    "PathField",
    "StatusBanner",
]
