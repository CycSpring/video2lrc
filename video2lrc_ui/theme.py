"""Native Qt styling for the video2lrc desktop application."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


COLORS = {
    "primary": "#0F766E",
    "primary_hover": "#0D6761",
    "primary_pressed": "#115E59",
    "focus": "#0D9488",
    "window": "#F4F6F7",
    "surface": "#FFFFFF",
    "surface_muted": "#EEF1F3",
    "border": "#C7CED4",
    "border_strong": "#98A4AE",
    "text": "#1F2933",
    "text_muted": "#52606D",
    "disabled": "#8A969F",
    "info": "#246B8A",
    "info_bg": "#EAF5F8",
    "success": "#237A57",
    "success_bg": "#EAF6EF",
    "warning": "#8A5A0A",
    "warning_bg": "#FFF6DD",
    "error": "#B42318",
    "error_bg": "#FDEDEA",
}


APP_STYLESHEET = f"""
QWidget {{
    color: {COLORS['text']};
    background-color: transparent;
    font-size: 13px;
}}

QMainWindow, QDialog {{
    background-color: {COLORS['window']};
}}

QFrame#headerBar, QFrame#runBar {{
    background-color: {COLORS['surface']};
}}

QFrame#headerBar {{
    border-bottom: 1px solid {COLORS['border']};
}}

QFrame#runBar {{
    border-top: 1px solid {COLORS['border']};
}}

QScrollArea#settingsScroll, QWidget#settingsPanel {{
    background-color: {COLORS['surface']};
}}

QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    background-color: {COLORS['surface']};
}}

QTabBar::tab {{
    min-width: 72px;
    min-height: 28px;
    padding: 0 10px;
    border: 1px solid transparent;
    background-color: transparent;
}}

QTabBar::tab:selected {{
    color: {COLORS['primary']};
    border-bottom-color: {COLORS['primary']};
    font-weight: 600;
}}

QSplitter::handle {{
    background-color: {COLORS['border']};
}}

QLabel[secondary="true"] {{
    color: {COLORS['text_muted']};
}}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: {COLORS['primary']};
    selection-color: #FFFFFF;
}}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    min-height: 22px;
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {COLORS['focus']};
}}

QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {COLORS['disabled']};
    background-color: {COLORS['surface_muted']};
}}

QPushButton {{
    min-height: 30px;
    padding: 0 12px;
    border: 1px solid {COLORS['border_strong']};
    border-radius: 4px;
    background-color: {COLORS['surface']};
}}

QPushButton:hover {{
    border-color: {COLORS['primary']};
    background-color: {COLORS['surface_muted']};
}}

QPushButton:pressed {{
    background-color: #E0E5E8;
}}

QPushButton:disabled {{
    color: {COLORS['disabled']};
    border-color: {COLORS['border']};
    background-color: {COLORS['surface_muted']};
}}

QPushButton[role="primary"] {{
    color: #FFFFFF;
    border-color: {COLORS['primary']};
    background-color: {COLORS['primary']};
    font-weight: 600;
}}

QPushButton[role="primary"]:hover {{
    border-color: {COLORS['primary_hover']};
    background-color: {COLORS['primary_hover']};
}}

QPushButton[role="primary"]:pressed {{
    border-color: {COLORS['primary_pressed']};
    background-color: {COLORS['primary_pressed']};
}}

QPushButton[role="primary"]:disabled {{
    color: {COLORS['disabled']};
    border-color: {COLORS['border']};
    background-color: {COLORS['surface_muted']};
}}

QToolButton {{
    border: 1px solid transparent;
    border-radius: 4px;
    background-color: transparent;
}}

QToolButton:hover {{
    border-color: {COLORS['border']};
    background-color: {COLORS['surface_muted']};
}}

QToolButton:pressed {{
    background-color: #E0E5E8;
}}

QToolButton#pathBrowseButton {{
    border-color: {COLORS['border']};
    background-color: {COLORS['surface']};
}}

QToolButton#sectionHeader {{
    border: 0;
    border-radius: 0;
    padding: 5px 2px;
    font-weight: 600;
}}

QToolButton#sectionHeader:hover {{
    background-color: {COLORS['surface_muted']};
}}

QFrame#previewFrame {{
    background-color: {COLORS['surface_muted']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    color: {COLORS['text_muted']};
}}

QFrame#statusBanner {{
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    background-color: {COLORS['surface']};
}}

QFrame#statusBanner[status="info"] {{
    color: {COLORS['info']};
    border-color: {COLORS['info']};
    background-color: {COLORS['info_bg']};
}}

QFrame#statusBanner[status="success"] {{
    color: {COLORS['success']};
    border-color: {COLORS['success']};
    background-color: {COLORS['success_bg']};
}}

QFrame#statusBanner[status="warning"] {{
    color: {COLORS['warning']};
    border-color: {COLORS['warning']};
    background-color: {COLORS['warning_bg']};
}}

QFrame#statusBanner[status="error"] {{
    color: {COLORS['error']};
    border-color: {COLORS['error']};
    background-color: {COLORS['error_bg']};
}}

QFrame#statusBanner QLabel {{
    color: inherit;
    background-color: transparent;
}}

QProgressBar {{
    min-height: 10px;
    max-height: 10px;
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    background-color: {COLORS['surface_muted']};
    text-align: center;
}}

QProgressBar::chunk {{
    border-radius: 3px;
    background-color: {COLORS['primary']};
}}

QToolTip {{
    color: {COLORS['text']};
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border_strong']};
    padding: 4px 6px;
}}
"""


def build_palette() -> QPalette:
    """Return the small neutral palette used alongside the application stylesheet."""

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS["surface_muted"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["primary"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(COLORS["disabled"]))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(COLORS["disabled"]),
    )
    return palette


def configure_high_dpi() -> None:
    """Use Qt's native high-DPI scaling with fractional scale factors."""

    if QApplication.instance() is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )


def apply_theme(app: QApplication) -> None:
    """Apply the shared palette and widget stylesheet to ``app``."""

    app.setPalette(build_palette())
    app.setStyleSheet(APP_STYLESHEET)


__all__ = [
    "APP_STYLESHEET",
    "COLORS",
    "apply_theme",
    "build_palette",
    "configure_high_dpi",
]
