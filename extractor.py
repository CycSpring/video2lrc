"""Video probing, ROI resolution, frame extraction, and ROI previews."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_FPS = 4
DEFAULT_CROP_BOTTOM_RATIO = 0.40


class MediaToolError(RuntimeError):
    """Raised when ffmpeg or ffprobe cannot be used."""


class VideoProbeError(RuntimeError):
    """Raised when ffprobe cannot produce usable video metadata."""


class FrameExtractionError(RuntimeError):
    """Raised when ffmpeg frame extraction fails."""


class ROIError(ValueError):
    """Raised when a normalized ROI is invalid."""


def _resolve_executable(command: str | Path) -> str:
    command_text = str(command)
    candidate = Path(command_text).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())

    # PyInstaller places bundled media tools below ``sys._MEIPASS`` in onedir
    # builds. A directly launched worker does not inherit the GUI's PATH setup,
    # so resolve that location before falling back to the host PATH.
    if bool(getattr(sys, "frozen", False)) and candidate.parent == Path("."):
        names = [candidate.name]
        if not candidate.suffix:
            names.append(f"{candidate.name}.exe")

        roots: list[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass) / "bin")
        executable_dir = Path(sys.executable).resolve().parent
        roots.extend((executable_dir / "bin", executable_dir / "_internal" / "bin"))

        for root in roots:
            for name in names:
                bundled = root / name
                if bundled.is_file():
                    return str(bundled.resolve())

    resolved = shutil.which(command_text)
    if resolved:
        return str(Path(resolved).resolve())

    raise MediaToolError(
        f"Required command '{command_text}' was not found. "
        "Install ffmpeg and make sure both ffmpeg and ffprobe are on PATH."
    )


def _run_version(command: str | Path) -> dict[str, str]:
    resolved = _resolve_executable(command)
    try:
        completed = subprocess.run(
            [resolved, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise MediaToolError(f"Cannot execute '{resolved}': {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise MediaToolError(f"Command '{resolved}' is not usable: {detail}")

    first_line = (completed.stdout or completed.stderr or "").splitlines()
    return {
        "command": str(command),
        "path": resolved,
        "version": first_line[0].strip() if first_line else "unknown",
    }


def ensure_media_tools(
    ffmpeg: str | Path = "ffmpeg", ffprobe: str | Path = "ffprobe"
) -> dict[str, dict[str, str]]:
    """Validate ffmpeg and ffprobe and return serializable version metadata."""

    return {
        "ffmpeg": _run_version(ffmpeg),
        "ffprobe": _run_version(ffprobe),
    }


def _to_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _duration_from_stream(stream: Mapping[str, Any]) -> float | None:
    direct = _to_float(stream.get("duration"))
    if direct is not None:
        return direct

    duration_ts = stream.get("duration_ts")
    time_base = stream.get("time_base")
    if duration_ts in (None, "N/A") or not time_base:
        return None
    try:
        return float(Fraction(str(duration_ts)) * Fraction(str(time_base)))
    except (ValueError, ZeroDivisionError):
        return None


def _rotation_from_stream(stream: Mapping[str, Any]) -> tuple[float, int]:
    raw_rotation: Any = None
    for side_data in stream.get("side_data_list") or ():
        if isinstance(side_data, Mapping) and side_data.get("rotation") is not None:
            raw_rotation = side_data["rotation"]
            break

    if raw_rotation is None:
        tags = stream.get("tags") or {}
        if isinstance(tags, Mapping):
            raw_rotation = tags.get("rotate")
    if raw_rotation is None:
        raw_rotation = stream.get("rotation", 0)

    parsed = _to_float(raw_rotation)
    parsed = 0.0 if parsed is None else parsed
    normalized = int(round(parsed)) % 360
    return parsed, normalized


def _frame_rate(stream: Mapping[str, Any]) -> tuple[str | None, float | None]:
    value = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not value or value == "0/0":
        return None, None
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return str(value), None
    return str(value), float(rate) if rate > 0 else None


def probe_video(
    video_path: str | Path,
    ffprobe: str | Path = "ffprobe",
    *,
    timeout: float | None = 30,
) -> dict[str, Any]:
    """Probe the first real video stream with ffprobe.

    Coded dimensions and display dimensions are both returned. Display dimensions
    account for the common 90/270-degree rotation metadata used by phone videos.
    """

    path = Path(video_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Input video path does not exist or is not a file: {path}")

    executable = _resolve_executable(ffprobe)
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path.resolve()),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProbeError(f"ffprobe timed out for input video: {path}") from exc
    except OSError as exc:
        raise MediaToolError(f"Cannot execute ffprobe command '{executable}': {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise VideoProbeError(f"ffprobe failed for '{path}': {detail}")

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoProbeError(f"ffprobe returned invalid JSON for '{path}': {exc}") from exc

    streams = payload.get("streams") or []
    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
    ]
    if not video_streams:
        raise VideoProbeError(f"Input contains no video stream: {path}")

    stream = next(
        (
            item
            for item in video_streams
            if not (item.get("disposition") or {}).get("attached_pic")
        ),
        video_streams[0],
    )
    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VideoProbeError(f"Video stream has invalid dimensions: {path}") from exc
    if width <= 0 or height <= 0:
        raise VideoProbeError(f"Video stream has invalid dimensions {width}x{height}: {path}")

    rotation_raw, rotation = _rotation_from_stream(stream)
    if rotation in (90, 270):
        display_width, display_height = height, width
    else:
        display_width, display_height = width, height

    format_info = payload.get("format") or {}
    duration = _duration_from_stream(stream)
    duration_source = "stream"
    if duration is None:
        duration = _to_float(format_info.get("duration"))
        duration_source = "format"
    if duration is None:
        duration = 0.0
        duration_source = "unknown"

    start_time = _to_float(stream.get("start_time"))
    start_time_source = "stream"
    if start_time is None:
        start_time = _to_float(format_info.get("start_time"))
        start_time_source = "format"
    if start_time is None:
        start_time = 0.0
        start_time_source = "default"

    frame_rate_raw, average_frame_rate = _frame_rate(stream)
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "stream_index": int(stream.get("index", 0)),
        "codec_name": stream.get("codec_name"),
        "width": width,
        "height": height,
        "coded_width": width,
        "coded_height": height,
        "display_width": display_width,
        "display_height": display_height,
        "rotation": rotation,
        "rotation_raw": rotation_raw,
        "duration": float(duration),
        "duration_source": duration_source,
        "start_time": float(start_time),
        "start_time_source": start_time_source,
        "average_frame_rate": average_frame_rate,
        "average_frame_rate_raw": frame_rate_raw,
        "time_base": stream.get("time_base"),
        "frame_count": int(stream["nb_frames"])
        if str(stream.get("nb_frames", "")).isdigit()
        else None,
        "probe_command": command,
    }
    return result


def parse_normalized_roi(value: str | Sequence[float]) -> tuple[float, float, float, float]:
    """Parse ``x,y,w,h`` normalized coordinates and validate their bounds."""

    if isinstance(value, str):
        parts: Iterable[Any] = (part.strip() for part in value.split(","))
    else:
        parts = value
    try:
        coordinates = tuple(float(part) for part in parts)
    except (TypeError, ValueError) as exc:
        raise ROIError("ROI must be four numbers in x,y,w,h form") from exc

    if len(coordinates) != 4:
        raise ROIError("ROI must contain exactly four numbers: x,y,w,h")
    x, y, width, height = coordinates
    if not all(math.isfinite(item) for item in coordinates):
        raise ROIError("ROI values must be finite numbers")
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ROIError("ROI x/y must be non-negative and width/height must be positive")
    tolerance = 1e-9
    if x + width > 1 + tolerance or y + height > 1 + tolerance:
        raise ROIError("ROI must stay inside normalized bounds 0..1")
    return x, y, width, height


def _pixel_edge(value: float, size: int) -> int:
    return int(math.floor(value * size + 0.5))


def resolve_roi(
    video_info: Mapping[str, Any],
    roi: str | Sequence[float] | None = None,
    crop_bottom_ratio: float | None = None,
) -> dict[str, Any]:
    """Resolve a normalized ROI against rotation-aware display dimensions."""

    try:
        frame_width = int(video_info.get("display_width") or video_info["width"])
        frame_height = int(video_info.get("display_height") or video_info["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ROIError("Video metadata must contain valid width and height") from exc
    if frame_width <= 0 or frame_height <= 0:
        raise ROIError("Video dimensions must be positive")

    if roi is not None:
        if crop_bottom_ratio is not None:
            raise ROIError("Explicit ROI and crop_bottom_ratio are mutually exclusive")
        normalized = parse_normalized_roi(roi)
        source = "roi"
    else:
        ratio = DEFAULT_CROP_BOTTOM_RATIO if crop_bottom_ratio is None else float(crop_bottom_ratio)
        if not math.isfinite(ratio) or ratio <= 0 or ratio > 1:
            raise ROIError("crop_bottom_ratio must be greater than 0 and at most 1")
        normalized = (0.0, 1.0 - ratio, 1.0, ratio)
        source = "crop_bottom_ratio"

    x, y, width, height = normalized
    left = min(frame_width - 1, max(0, _pixel_edge(x, frame_width)))
    top = min(frame_height - 1, max(0, _pixel_edge(y, frame_height)))
    right = min(frame_width, max(left + 1, _pixel_edge(x + width, frame_width)))
    bottom = min(frame_height, max(top + 1, _pixel_edge(y + height, frame_height)))
    pixel_width = right - left
    pixel_height = bottom - top

    return {
        "source": source,
        "normalized": {"x": x, "y": y, "width": width, "height": height},
        "pixels": {
            "x": left,
            "y": top,
            "width": pixel_width,
            "height": pixel_height,
        },
        "x": left,
        "y": top,
        "width": pixel_width,
        "height": pixel_height,
        "frame_width": frame_width,
        "frame_height": frame_height,
    }


def _fps_fraction(fps: int | float | str | Fraction) -> Fraction:
    try:
        value = fps if isinstance(fps, Fraction) else Fraction(str(fps))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid fps value: {fps}") from exc
    if value <= 0:
        raise ValueError("fps must be greater than zero")
    return value.limit_denominator(1_000_000)


def _fps_text(fps: int | float | str | Fraction) -> str:
    value = _fps_fraction(fps)
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def frame_time_fraction(frame_number: int, fps: int | float | str | Fraction) -> Fraction:
    """Return an exact zero-based timestamp for a one-based output frame number."""

    if frame_number < 1:
        raise ValueError("frame_number must be one-based and greater than zero")
    return Fraction(frame_number - 1, 1) / _fps_fraction(fps)


def frame_time(frame_number: int, fps: int | float | str | Fraction) -> float:
    return float(frame_time_fraction(frame_number, fps))


def build_frame_index(
    frames: str | Path | Sequence[str | Path],
    fps: int | float | str | Fraction = DEFAULT_FPS,
) -> list[dict[str, Any]]:
    """Build a deterministic time index for extracted PNG frames."""

    if isinstance(frames, (str, Path)):
        directory = Path(frames)
        paths = (
            sorted(
                item
                for item in directory.glob("*.png")
                if item.stem.isdigit() and len(item.stem) == 6
            )
            if directory.is_dir()
            else [directory]
        )
    else:
        paths = sorted((Path(item) for item in frames), key=lambda item: item.name)

    result: list[dict[str, Any]] = []
    for sequence_number, path in enumerate(paths, start=1):
        frame_number = int(path.stem) if path.stem.isdigit() else sequence_number
        exact_time = frame_time_fraction(frame_number, fps)
        result.append(
            {
                "frame": path.name,
                "path": str(path.resolve()),
                "frame_number": frame_number,
                "time": float(exact_time),
                "time_fraction": str(exact_time),
            }
        )
    return result


def build_filter_chain(
    resolved_roi: Mapping[str, Any],
    fps: int | float | str | Fraction = DEFAULT_FPS,
) -> str:
    pixels = resolved_roi.get("pixels", resolved_roi)
    try:
        x = int(pixels["x"])
        y = int(pixels["y"])
        width = int(pixels["width"])
        height = int(pixels["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ROIError("Resolved ROI must contain integer x/y/width/height") from exc
    if min(width, height) <= 0 or min(x, y) < 0:
        raise ROIError("Resolved ROI pixels are invalid")
    return (
        f"setpts=PTS-STARTPTS,crop={width}:{height}:{x}:{y},"
        f"fps={_fps_text(fps)}"
    )


def _remove_stale_frames(output_dir: Path) -> None:
    for path in output_dir.glob("*.png"):
        if path.stem.isdigit() and len(path.stem) == 6:
            path.unlink()


def extract_frames(
    video_path: str | Path,
    output_dir: str | Path,
    roi: str | Sequence[float] | None = None,
    fps: int | float | str | Fraction = DEFAULT_FPS,
    *,
    crop_bottom_ratio: float | None = None,
    ffmpeg: str | Path = "ffmpeg",
    ffprobe: str | Path = "ffprobe",
    video_info: Mapping[str, Any] | None = None,
    overwrite: bool = True,
    timeout: float | None = None,
    cancellation_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Extract cropped PNG frames using one ffmpeg filter chain."""

    path = Path(video_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Input video path does not exist or is not a file: {path}")
    fps_value = _fps_fraction(fps)
    metadata = dict(video_info) if video_info is not None else probe_video(path, ffprobe)
    resolved_roi = resolve_roi(metadata, roi, crop_bottom_ratio)
    filter_chain = build_filter_chain(resolved_roi, fps_value)
    executable = _resolve_executable(ffmpeg)

    frames_dir = Path(output_dir).expanduser().resolve()
    frames_dir.mkdir(parents=True, exist_ok=True)
    existing = [item for item in frames_dir.glob("*.png") if item.stem.isdigit()]
    if existing and not overwrite:
        raise FileExistsError(f"Frame output directory already contains frames: {frames_dir}")
    if overwrite:
        _remove_stale_frames(frames_dir)

    pattern = frames_dir / "%06d.png"
    stream_index = metadata.get("stream_index")
    map_specifier = f"0:{int(stream_index)}" if stream_index is not None else "0:v:0"
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-autorotate",
        "-i",
        str(path.resolve()),
        "-map",
        map_specifier,
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filter_chain,
        "-start_number",
        "1",
        str(pattern),
    ]
    try:
        if cancellation_callback is None:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        else:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            started_at = time.monotonic()
            try:
                while True:
                    cancellation_callback()
                    wait_seconds = 0.1
                    if timeout is not None:
                        remaining = timeout - (time.monotonic() - started_at)
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        wait_seconds = min(wait_seconds, remaining)
                    try:
                        stdout, stderr = process.communicate(timeout=wait_seconds)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                raise
            completed = subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout,
                stderr,
            )
    except subprocess.TimeoutExpired as exc:
        raise FrameExtractionError(f"ffmpeg timed out for input video: {path}") from exc
    except OSError as exc:
        raise MediaToolError(f"Cannot execute ffmpeg command '{executable}': {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise FrameExtractionError(f"ffmpeg failed for '{path}': {detail}")

    frame_paths = sorted(
        item
        for item in frames_dir.glob("*.png")
        if item.stem.isdigit() and len(item.stem) == 6
    )
    if not frame_paths:
        raise FrameExtractionError(
            f"ffmpeg completed but produced no frames in output directory: {frames_dir}"
        )
    frame_index = build_frame_index(frame_paths, fps_value)
    return {
        "video": metadata,
        "frames_dir": str(frames_dir),
        "frames": [str(item.resolve()) for item in frame_paths],
        "frame_count": len(frame_paths),
        "frame_index": frame_index,
        "fps": float(fps_value),
        "fps_fraction": str(fps_value),
        "roi": resolved_roi,
        "filter": filter_chain,
        "command": command,
    }


def _import_preview_dependencies() -> tuple[Any, Any]:
    try:
        import cv2  # type: ignore
        import numpy  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "ROI preview requires OpenCV and NumPy; install opencv-python and numpy."
        ) from exc
    return cv2, numpy


def _orient_preview_frame(frame: Any, video_info: Mapping[str, Any], cv2: Any) -> Any:
    rotation = int(video_info.get("rotation", 0)) % 360
    if rotation not in (90, 180, 270):
        return frame

    height, width = frame.shape[:2]
    display_size = (
        int(video_info.get("display_width", width)),
        int(video_info.get("display_height", height)),
    )
    if (width, height) == display_size:
        return frame
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)


def _preview_sample_times(duration: float, sample_count: int) -> list[float]:
    if sample_count < 1:
        raise ValueError("sample_count must be greater than zero")
    if duration <= 0:
        return [float(index) for index in range(sample_count)]
    if sample_count == 1:
        return [duration / 2]
    start = duration * 0.05
    span = duration * 0.90
    return [start + span * index / (sample_count - 1) for index in range(sample_count)]


def _write_cv_image(path: Path, image: Any, cv2: Any) -> None:
    suffix = path.suffix.lower() or ".png"
    if suffix == ".jpeg":
        suffix = ".jpg"
    success, encoded = cv2.imencode(suffix, image)
    if not success:
        raise RuntimeError(f"OpenCV cannot encode ROI preview as '{suffix}'")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded.tofile(str(path))


def create_roi_preview(
    video_path: str | Path,
    output_path: str | Path,
    roi: str | Sequence[float] | None = None,
    *,
    crop_bottom_ratio: float | None = None,
    sample_count: int = 6,
    thumbnail_width: int = 480,
    video_info: Mapping[str, Any] | None = None,
    ffprobe: str | Path = "ffprobe",
) -> dict[str, Any]:
    """Draw the ROI on samples from the beginning, middle, and end of a video."""

    path = Path(video_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Input video path does not exist or is not a file: {path}")
    if thumbnail_width < 64:
        raise ValueError("thumbnail_width must be at least 64 pixels")

    metadata = dict(video_info) if video_info is not None else probe_video(path, ffprobe)
    resolved_roi = resolve_roi(metadata, roi, crop_bottom_ratio)
    cv2, np = _import_preview_dependencies()
    capture = cv2.VideoCapture(str(path.resolve()))
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"OpenCV cannot open input video for ROI preview: {path}")

    if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    duration = float(metadata.get("duration") or 0.0)
    if duration <= 0 and hasattr(cv2, "CAP_PROP_FRAME_COUNT"):
        count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        rate = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        if count > 0 and rate > 0:
            duration = count / rate
    sample_times = _preview_sample_times(duration, int(sample_count))

    annotated_frames: list[Any] = []
    sample_records: list[dict[str, Any]] = []
    normalized = resolved_roi["normalized"]
    try:
        for sample_time in sample_times:
            capture.set(cv2.CAP_PROP_POS_MSEC, sample_time * 1000)
            ok, frame = capture.read()
            if not ok or frame is None:
                sample_records.append(
                    {"time": sample_time, "status": "read_error", "error": "frame unavailable"}
                )
                continue

            frame = _orient_preview_frame(frame, metadata, cv2)
            frame_height, frame_width = frame.shape[:2]
            current_roi = resolve_roi(
                {"display_width": frame_width, "display_height": frame_height},
                (
                    normalized["x"],
                    normalized["y"],
                    normalized["width"],
                    normalized["height"],
                ),
                None,
            )
            pixels = current_roi["pixels"]
            x1, y1 = pixels["x"], pixels["y"]
            x2 = min(frame_width - 1, x1 + pixels["width"] - 1)
            y2 = min(frame_height - 1, y1 + pixels["height"] - 1)
            annotated = frame.copy()
            line_width = max(2, round(min(frame_width, frame_height) / 300))
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (40, 220, 80), line_width)
            cv2.putText(
                annotated,
                f"{sample_time:.2f}s",
                (12, 32),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (40, 220, 80),
                2,
                cv2.LINE_AA,
            )
            target_height = max(1, round(frame_height * thumbnail_width / frame_width))
            annotated = cv2.resize(
                annotated, (thumbnail_width, target_height), interpolation=cv2.INTER_AREA
            )
            annotated_frames.append(annotated)
            sample_records.append(
                {
                    "time": sample_time,
                    "status": "ok",
                    "frame_width": frame_width,
                    "frame_height": frame_height,
                    "roi": pixels,
                }
            )
    finally:
        capture.release()

    if not annotated_frames:
        raise RuntimeError(f"No preview frames could be read from input video: {path}")

    columns = min(3, len(annotated_frames))
    rows = math.ceil(len(annotated_frames) / columns)
    cell_height = max(item.shape[0] for item in annotated_frames)
    gap = 8
    canvas_height = rows * cell_height + (rows + 1) * gap
    canvas_width = columns * thumbnail_width + (columns + 1) * gap
    canvas = np.full((canvas_height, canvas_width, 3), 24, dtype=np.uint8)
    for index, frame in enumerate(annotated_frames):
        row, column = divmod(index, columns)
        top = gap + row * (cell_height + gap)
        left = gap + column * (thumbnail_width + gap)
        canvas[top : top + frame.shape[0], left : left + frame.shape[1]] = frame

    destination = Path(output_path).expanduser().resolve()
    _write_cv_image(destination, canvas, cv2)
    return {
        "output_path": str(destination),
        "video": metadata,
        "roi": resolved_roi,
        "sample_count": len(annotated_frames),
        "samples": sample_records,
        "canvas_width": canvas_width,
        "canvas_height": canvas_height,
    }


preview_roi = create_roi_preview


__all__ = [
    "DEFAULT_CROP_BOTTOM_RATIO",
    "DEFAULT_FPS",
    "FrameExtractionError",
    "MediaToolError",
    "ROIError",
    "VideoProbeError",
    "build_filter_chain",
    "build_frame_index",
    "create_roi_preview",
    "ensure_media_tools",
    "extract_frames",
    "frame_time",
    "frame_time_fraction",
    "parse_normalized_roi",
    "preview_roi",
    "probe_video",
    "resolve_roi",
]
