from __future__ import annotations

import ast
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _assigned_call(tree: ast.Module, variable: str) -> ast.Call:
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == variable
            and isinstance(node.value, ast.Call)
        ):
            return node.value
    raise AssertionError(f"call assignment {variable!r} was not found")


def _keyword(call: ast.Call, name: str) -> ast.expr:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"keyword {name!r} was not found")


def test_spec_builds_gui_and_cli_into_one_shared_collect() -> None:
    source = _source("video2lrc.spec")
    tree = ast.parse(source, filename="video2lrc.spec")

    gui_analysis = _assigned_call(tree, "gui_analysis")
    cli_analysis = _assigned_call(tree, "cli_analysis")
    assert isinstance(gui_analysis.func, ast.Name)
    assert gui_analysis.func.id == "Analysis"
    assert "gui.py" in ast.unparse(gui_analysis.args[0])
    assert isinstance(cli_analysis.func, ast.Name)
    assert cli_analysis.func.id == "Analysis"
    assert "main.py" in ast.unparse(cli_analysis.args[0])

    gui_exe = _assigned_call(tree, "gui_exe")
    cli_exe = _assigned_call(tree, "cli_exe")
    assert ast.literal_eval(_keyword(gui_exe, "name")) == "Video2LRC"
    assert ast.literal_eval(_keyword(gui_exe, "console")) is False
    assert "VERSION_FILE" in ast.unparse(_keyword(gui_exe, "version"))
    assert "ICON_FILE" in ast.unparse(_keyword(gui_exe, "icon"))
    assert ast.literal_eval(_keyword(cli_exe, "name")) == "video2lrc-cli"
    assert ast.literal_eval(_keyword(cli_exe, "console")) is True
    assert "VERSION_FILE" in ast.unparse(_keyword(cli_exe, "version"))
    assert "ICON_FILE" in ast.unparse(_keyword(cli_exe, "icon"))
    assert all(keyword.arg != "contents_directory" for keyword in gui_exe.keywords)
    assert all(keyword.arg != "contents_directory" for keyword in cli_exe.keywords)

    collect = _assigned_call(tree, "bundle")
    assert isinstance(collect.func, ast.Name)
    assert collect.func.id == "COLLECT"
    collect_arguments = {
        argument.id for argument in collect.args if isinstance(argument, ast.Name)
    }
    assert {"gui_exe", "cli_exe"} <= collect_arguments
    assert ast.literal_eval(_keyword(collect, "name")) == "Video2LRC"


def test_spec_has_optional_ffmpeg_and_onnx_only_backend_policy() -> None:
    source = _source("video2lrc.spec")

    assert "VIDEO2LRC_FFMPEG" in source
    assert "VIDEO2LRC_FFPROBE" in source
    assert 'return [(str(source), "bin")]' in source
    assert 'collect_submodules(\n    "rapidocr.inference_engine.onnxruntime"' in source
    for backend in ("mnn", "openvino", "paddle", "pytorch", "tensorrt"):
        assert f'"rapidocr.inference_engine.{backend}"' in source


def test_application_icon_assets_cover_windows_sizes() -> None:
    png_path = PROJECT_ROOT / "assets" / "video2lrc-icon.png"
    ico_path = PROJECT_ROOT / "assets" / "video2lrc.ico"

    with Image.open(png_path) as png:
        assert png.format == "PNG"
        assert png.mode == "RGBA"
        assert png.size == (1024, 1024)
        assert png.getpixel((0, 0))[3] == 0

    with Image.open(ico_path) as ico:
        assert ico.format == "ICO"
        assert {(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)} <= ico.ico.sizes()


def test_rapidocr_hook_collects_models_configs_and_metadata() -> None:
    source = _source("packaging/hooks/hook-rapidocr.py")
    ast.parse(source, filename="hook-rapidocr.py")

    assert "collect_data_files" in source
    assert '"config.yaml"' in source
    assert '"default_models.yaml"' in source
    assert '"models/*.onnx"' in source
    assert 'copy_metadata("rapidocr")' in source
    assert 'copy_metadata("onnxruntime")' in source
    assert '"rapidocr.inference_engine.onnxruntime.main"' in source
    assert "excludedimports" in source


def test_build_script_is_pinned_noninteractive_and_validates_outputs() -> None:
    source = _source("build.ps1")

    assert "[switch]$BundleFFmpeg" in source
    assert '"3.11"' in source
    assert '"requirements.lock.txt"' in source
    assert '"--no-input"' in source
    assert "$PythonCommand = @(Resolve-PythonCommand" in source
    assert '"PyInstaller"' in source
    assert '"Video2LRC.exe"' in source
    assert '"video2lrc-cli.exe"' in source
    assert '"_internal"' in source
    assert '"onnxruntime.dll"' in source
    assert '"qwindows.dll"' in source
    assert "video2lrc-icon.png" in source
    assert '"--help"' in source
    assert '"--screenshot"' in source
    assert '"QT_QPA_PLATFORM", "offscreen"' in source
    assert "WaitForExit(30000)" in source
    assert "finally {" in source
    assert "$originalEnvironment[$environmentName]" in source
    assert "Invoke-Expression" not in source

    install_block = source.split(
        '-Description "Build dependency installation"', 1
    )[1].split('-Description "Test suite"', 1)[0]
    assert '"pytest"' not in install_block


def test_build_requirements_aliases_the_verified_lock() -> None:
    build_requirements = _source("requirements-build.txt")
    assert "-r requirements.lock.txt" in build_requirements
    assert "PyInstaller==" not in build_requirements

    lock = _source("requirements.lock.txt")
    for package in (
        "numpy",
        "onnxruntime",
        "opencv-python",
        "pytest",
        "pyinstaller",
        "PySide6-Essentials",
        "rapidocr",
    ):
        assert any(
            line.lower().startswith(f"{package.lower()}==")
            for line in lock.splitlines()
        )


def test_version_resource_contains_product_and_numeric_version() -> None:
    source = _source("packaging/version_info.txt")
    ast.parse(source, filename="version_info.txt")
    assert 'StringStruct("ProductName", "Video2LRC")' in source
    assert 'StringStruct("FileVersion", "0.1.0.0")' in source
    assert "filevers=(0, 1, 0, 0)" in source


def test_release_packager_requires_self_contained_assets_and_checksums() -> None:
    source = _source("package-release.ps1")

    assert '"_internal\\bin\\ffmpeg.exe"' in source
    assert '"_internal\\bin\\ffprobe.exe"' in source
    assert "Pass -FfmpegLicensePath" in source
    assert "[string]$Python" in source
    assert "Resolve-PythonExecutable" in source
    assert "sysconfig.get_paths()['purelib']" in source
    assert "FFmpeg-GPL-3.0.txt" in source
    assert "THIRD_PARTY_NOTICES.md" in source
    assert "requirements.lock.txt" in source
    assert "windows-x64-portable.zip" in source
    assert "windows-x64-setup.exe" in source
    assert "SHA256SUMS.txt" in source
    assert "Get-FileHash" in source
    assert "$isccOutput" in source
    assert "Select-Object -Last 30" in source
    assert "Remove-Item -LiteralPath $ReleaseRoot -Recurse -Force" in source
    assert "StartsWith($projectFullPath" in source


def test_inno_installer_is_per_user_x64_and_reproducible() -> None:
    source = _source("installer/Video2LRC.iss")

    assert "AppId={{2E31B2DF-1961-4C78-88B8-329AFB4FA049}" in source
    assert "PrivilegesRequired=lowest" in source
    assert "ArchitecturesAllowed=x64compatible" in source
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in source
    assert "Source: \"{#SourceDir}\\*\"" in source
    assert "recursesubdirs" in source
    assert "UninstallDisplayIcon={app}\\{#MyAppExeName}" in source
    assert "Video2LRC-v{#MyAppVersion}-windows-x64-setup" in source


def test_release_workflow_pins_external_build_inputs() -> None:
    source = _source(".github/workflows/release.yml")

    assert 'tags:\n      - "v*"' in source
    assert "contents: write" in source
    assert "actions/checkout@v7" in source
    assert "actions/setup-python@v6" in source
    assert "actions/upload-artifact@v7" in source
    assert "ffmpeg-8.1.1-essentials_build.zip" in source
    assert "6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541" in source
    assert "innosetup-6.7.3.exe" in source
    assert "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732" in source
    assert ".\\build.ps1" in source
    assert ".\\package-release.ps1" in source
    assert "::error title=Release packaging failed::" in source
    assert 'gh release create "${{ github.ref_name }}"' in source
