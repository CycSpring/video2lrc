"""Application bootstrap for the native Video2LRC desktop UI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .theme import apply_theme, configure_high_dpi


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Video2LRC", add_help=True)
    parser.add_argument("--video", type=Path, help="启动时载入的视频")
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--size", default="1180x760", help=argparse.SUPPRESS)
    return parser


def _parse_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT") from exc
    if width < 720 or height < 600:
        raise argparse.ArgumentTypeError("size must be at least 720x600")
    return width, height


def application_icon_path() -> Path:
    """Return the source or PyInstaller path to the runtime application icon."""

    bundled_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundled_root) if bundled_root else Path(__file__).resolve().parents[1]
    return root / "assets" / "video2lrc-icon.png"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        size = _parse_size(args.size)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    configure_high_dpi()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("Video2LRC")
    app.setApplicationDisplayName("Video2LRC")
    app.setOrganizationName("Video2LRC")
    app.setOrganizationDomain("local.video2lrc")
    app.setQuitOnLastWindowClosed(True)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    application_icon = QIcon(str(application_icon_path()))
    if not application_icon.isNull():
        app.setWindowIcon(application_icon)
    apply_theme(app)

    window = MainWindow(args.video)
    if not application_icon.isNull():
        window.setWindowIcon(application_icon)
    window.resize(*size)
    window.show()

    if args.screenshot is not None:
        target = args.screenshot.expanduser().resolve()

        def capture() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(target)):
                app.exit(2)
                return
            app.quit()

        QTimer.singleShot(350, capture)

    return app.exec()


__all__ = ["application_icon_path", "build_parser", "main"]
