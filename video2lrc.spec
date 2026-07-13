# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


PROJECT_ROOT = Path(SPECPATH).resolve()
HOOKS_DIR = PROJECT_ROOT / "packaging" / "hooks"
VERSION_FILE = PROJECT_ROOT / "packaging" / "version_info.txt"
ICON_FILE = PROJECT_ROOT / "assets" / "video2lrc.ico"
ICON_PNG = PROJECT_ROOT / "assets" / "video2lrc-icon.png"


def optional_tool_binary(environment_name, executable_name):
    """Collect an explicitly configured media tool in the shared bin folder."""

    configured = os.environ.get(environment_name, "").strip()
    if not configured:
        return []

    source = Path(configured).expanduser()
    if source.is_dir():
        source = source / executable_name
    source = source.resolve()
    if not source.is_file():
        raise SystemExit(
            f"{environment_name} does not point to a file: {source}"
        )
    if source.name.lower() != executable_name.lower():
        raise SystemExit(
            f"{environment_name} must point to {executable_name}: {source}"
        )
    return [(str(source), "bin")]


media_binaries = [
    *optional_tool_binary("VIDEO2LRC_FFMPEG", "ffmpeg.exe"),
    *optional_tool_binary("VIDEO2LRC_FFPROBE", "ffprobe.exe"),
]

# RapidOCR loads its selected inference backend conditionally. Only ONNX Runtime
# is used by this application; keeping the other backend trees out materially
# reduces both analysis time and the shipped directory size.
rapidocr_hiddenimports = collect_submodules(
    "rapidocr.inference_engine.onnxruntime"
)
unused_backends = [
    "rapidocr.inference_engine.mnn",
    "rapidocr.inference_engine.openvino",
    "rapidocr.inference_engine.paddle",
    "rapidocr.inference_engine.pytorch",
    "rapidocr.inference_engine.tensorrt",
    "MNN",
    "openvino",
    "paddle",
    "paddlepaddle",
    "tensorrt",
    "torch",
    "torchvision",
]

analysis_options = {
    "pathex": [str(PROJECT_ROOT)],
    "binaries": media_binaries,
    "datas": [(str(ICON_PNG), "assets")],
    "hiddenimports": rapidocr_hiddenimports,
    "hookspath": [str(HOOKS_DIR)],
    "hooksconfig": {},
    "runtime_hooks": [],
    "excludes": unused_backends,
    "noarchive": False,
    "optimize": 0,
}

gui_analysis = Analysis(
    [str(PROJECT_ROOT / "gui.py")],
    **analysis_options,
)
cli_analysis = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    **analysis_options,
)

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="Video2LRC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(VERSION_FILE),
    icon=str(ICON_FILE),
)
cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="video2lrc-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(VERSION_FILE),
    icon=str(ICON_FILE),
)

# Both launchers intentionally share one onedir payload. PyInstaller's default
# _internal directory contains the common runtime and optional bin directory.
bundle = COLLECT(
    gui_exe,
    cli_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Video2LRC",
)
