# video2lrc

[English](README.md) | [简体中文](README.zh-CN.md)

`video2lrc` extracts burned-in lyric subtitles from a music video and writes a line-level LRC draft. The MVP is designed for CPU-only Windows use and targets an editable result rather than frame-perfect karaoke timing.

## Install the Windows release

Download the current assets from the [GitHub Releases page](https://github.com/CycSpring/video2lrc/releases):

- `Video2LRC-v0.1.0-windows-x64-setup.exe` installs the application for the current Windows user.
- `Video2LRC-v0.1.0-windows-x64-portable.zip` is the equivalent no-install package.
- `SHA256SUMS.txt` contains checksums for both downloads.

The release assets include `ffmpeg` and `ffprobe`, so end users do not need to configure them separately. The executables are not Authenticode-signed; Windows SmartScreen may therefore show an unknown-publisher warning.

## Native desktop UI

The desktop application uses native Qt Widgets through `PySide6-Essentials`. It does not use Electron, Chromium, Qt WebEngine, or an embedded web server. The UI stays lightweight and starts the existing CLI in a separate `QProcess`, so OCR and ONNX memory is released when a job finishes.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
.\.venv\Scripts\python.exe gui.py
```

The UI provides:

- Video and output selection, including drag and drop.
- Bottom-crop and normalized custom ROI modes with a generated preview montage.
- Sampling, worker, subtitle-style, active-row, offset, confidence, and detector controls.
- One-click restoration of all recommended processing defaults without clearing file paths.
- Structured stage/OCR progress, ANSI-clean bounded logs with routine empty-frame messages collapsed, cooperative cancellation, and a process-tree fallback.
- LRC preview, copy/open actions, persistent settings, and cache reuse.

The GUI never imports RapidOCR, ONNX Runtime, or OpenCV itself. A worker CLI process owns those dependencies and communicates with the window through prefixed JSONL events.

## What it produces

- A plain UTF-8 LRC file using `[mm:ss.xx]lyrics` lines and no metadata tags.
- Likely phrase spacing is retained when the same OCR text-block boundary is observed across frames.
- `review.csv` for checking confidence, support frames, timestamps, and QA flags in Excel.
- JSON stage caches under `work/<run_id>/` so detector or offset changes do not repeat OCR.
- An optional multi-timestamp ROI preview before a full run.

The default timestamp is the first stable sampled appearance of a subtitle line. Use `--offset-ms` to compensate for subtitles that appear before or after the vocal.

## Requirements

- Windows with Python 3.11 or 3.12.
- `ffmpeg` and `ffprobe` available on `PATH`.
- CPU inference; no GPU runtime is required.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
ffmpeg -version
```

`requirements.txt` keeps direct dependencies intentionally broad for source development. Release builds always install the exact Python 3.11 environment in `requirements.lock.txt`; `requirements-build.txt` is a convenience alias for that lock file.

## Start with an ROI preview

The default OCR area is the bottom 40% of the video. Check it before processing a full song:

```powershell
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --preview-roi
```

For subtitles outside that area, provide normalized `x,y,width,height` coordinates:

```powershell
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --roi 0.08,0.64,0.84,0.24 --preview-roi
```

## Generate an LRC draft

```powershell
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" -o "D:\Videos\song.lrc"
```

Common adjustments:

```powershell
# Reuse OCR and move every line 350 ms later.
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --resume --offset-ms 350 --force

# Sample more densely and keep frame images for diagnosis.
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --fps 8 --keep-workdir

# Select the bottom active row in a two-line subtitle layout.
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --active-row bottom

# Preview detected lines without writing the final LRC.
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --dry-run
```

Run `python main.py --help` for all detector, confidence, cache, and output controls.

## Build the Windows application

The supported package shape is a PyInstaller `onedir` directory with two launchers sharing one dependency payload:

```text
dist\Video2LRC\
  Video2LRC.exe
  video2lrc-cli.exe
  _internal\
```

Build a thin package that uses FFmpeg from `PATH`:

```powershell
.\build.ps1
```

Or bundle explicit FFmpeg and FFprobe executables:

```powershell
.\build.ps1 -BundleFFmpeg -FfmpegPath "C:\Tools\ffmpeg.exe" -FfprobePath "C:\Tools\ffprobe.exe"
```

Bundled media tools are resolved from `_internal\bin` by both the GUI worker and direct invocations of `video2lrc-cli.exe`; they do not require a system FFmpeg installation.

Create the installer and portable release assets after a bundled build:

```powershell
.\package-release.ps1 -Version 0.1.0 -FfmpegLicensePath "C:\path\to\ffmpeg\LICENSE"
```

The release packager copies runtime license texts into the package, creates both assets with Inno Setup and `tar`, and writes `release\SHA256SUMS.txt`. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution and source links.

Pushing a `vX.Y.Z` tag runs [the release workflow](.github/workflows/release.yml), which downloads hash-pinned FFmpeg and Inno Setup inputs, repeats the full build, and publishes the three verified assets to GitHub Releases.

The build validates Python 3.11, installs `requirements.lock.txt`, runs the full test suite, collects the RapidOCR YAML and ONNX model files, and verifies Qt, ONNX Runtime, optional media tools, executable version resources, a frozen CLI launch, and an offscreen GUI screenshot. It restores every process environment variable it temporarily changes.

Before publishing a release, also run a real short-video OCR job on a clean Windows machine. That media-dependent check is intentionally not a universal build prerequisite. The generated executables contain product/version metadata but are not code-signed. The release packager collects available third-party license texts, but maintainers should still review notices whenever dependencies or the selected FFmpeg build change.

See [UI_PLAN.md](UI_PLAN.md) for the architecture and acceptance criteria.

## Cache behavior

Each run stores a `manifest.json` with separate keys for extraction, OCR, detection, and writing:

- Changing only `--offset-ms` reuses `lines.json`.
- Changing detector thresholds reuses `ocr_raw.json`.
- Changing FPS or ROI invalidates extraction and every downstream stage.
- Changing the OCR package/model version invalidates OCR and downstream stages.

On a successful run, extracted PNG frames are removed while JSON and CSV diagnostics remain unless `--keep-workdir` is enabled. Recursive cleanup is restricted to the selected work root's reserved `<work_root>/<run_id>/frames` directory, including when a custom `--workdir` is used.

Final LRC output must be outside the selected work root, so `--force` cannot overwrite an input video or any run cache. Each run holds an exclusive `.run.lock`; a second process targeting the same resume cache fails instead of mixing frames or manifests. Stage keys are invalidated before artifacts are replaced, so an interrupted parameter change cannot make an old key point at new data.

## Evaluation

`evaluator.py` compares generated output with a reference LRC by order-preserving text alignment. It reports line recall/precision, CER, estimated global offset, residual median/P90 timing error, and manual edit rate. Manual edit rate uses the reference-line count as its denominator; missing lines, incorrect matched lines, and extra candidate lines each count as one edit, so severely noisy output can exceed 100%. Reference lyrics must be from the same video edit for timing metrics to be meaningful.

```powershell
.\.venv\Scripts\python.exe evaluator.py reference.lrc candidate.lrc -o report.json
```

Existing reports are not overwritten unless `--force` is supplied; the report path can never be either input LRC.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Unit tests use fixed OCR dictionaries and mocked media commands. Put private integration clips under `local_testdata/`; that directory and generated LRC files are ignored by Git.

## MVP limits

- Timing precision is bounded by sampling rate: 4 FPS implies up to roughly 250 ms sampling error.
- Two-line subtitles require a focused ROI or explicit `--active-row`; the MVP does not infer the currently sung row reliably.
- Stable adjacent lines are preserved even when their OCR text is highly similar; `similar_to_previous` flags the ambiguity for review instead of silently merging a potentially real lyric.
- The output is line-level LRC, not per-character karaoke timing.
- The default `line` mode is the supported path. `typewriter` and `auto` are auxiliary modes.

See [PLAN.md](PLAN.md) for the complete design, acceptance thresholds, and follow-up roadmap.
