"""Windowed entry point for the native Video2LRC desktop application."""

from __future__ import annotations

import multiprocessing

from video2lrc_ui.app import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
