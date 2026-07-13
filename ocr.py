"""RapidOCR compatibility and multiprocessing helpers.

The module deliberately imports RapidOCR only when OCR is requested. On Windows,
call :func:`ocr_all_frames` from an ``if __name__ == "__main__"`` guarded entry
point when more than one worker is used.
"""

from __future__ import annotations

import concurrent.futures
import importlib.metadata
import inspect
import math
import os
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_WORKERS = 4
DEFAULT_ORT_THREADS = 1
FUTURE_POLL_INTERVAL_SECONDS = 0.1

_WORKER_ENGINE: Any = None


class OCRDependencyError(ImportError):
    """Raised when RapidOCR or its runtime backend is unavailable."""


class OCREngineError(RuntimeError):
    """Raised when the OCR engine cannot be initialized."""


class OCRFrameError(RuntimeError):
    """Raised in fail-fast mode when an individual frame cannot be processed."""


def _load_rapidocr_class() -> type[Any]:
    try:
        from rapidocr import RapidOCR
    except (ImportError, OSError) as exc:
        raise OCRDependencyError(
            "RapidOCR is unavailable. Install the 'rapidocr' and 'onnxruntime' packages."
        ) from exc
    return RapidOCR


def ocr_runtime_info() -> dict[str, str | None]:
    """Return installed OCR package versions without importing model code."""

    result: dict[str, str | None] = {}
    for package in ("rapidocr", "onnxruntime"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def _supports_parameter(callable_object: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(callable_object).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters


def _accepts_keyword(callable_object: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(callable_object).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _resolve_text_score(
    engine_kwargs: Mapping[str, Any] | None,
    explicit: float | None,
) -> float | None:
    value: Any = explicit
    if value is None and engine_kwargs:
        value = engine_kwargs.get("Global.text_score")
        nested_params = engine_kwargs.get("params")
        if value is None and isinstance(nested_params, Mapping):
            value = nested_params.get("Global.text_score")
    if value is None:
        return None
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("text_score must be between 0 and 1")
    return score


def create_ocr_engine(
    engine_kwargs: Mapping[str, Any] | None = None,
    *,
    ort_threads: int = DEFAULT_ORT_THREADS,
) -> Any:
    """Create a RapidOCR engine across legacy and current constructor APIs."""

    if ort_threads < 1:
        raise ValueError("ort_threads must be at least one")
    rapidocr_class = _load_rapidocr_class()
    options = dict(engine_kwargs or {})

    try:
        if _supports_parameter(rapidocr_class, "params"):
            config_path = options.pop("config_path", None)
            supplied_params = options.pop("params", None)
            if supplied_params is not None and not isinstance(supplied_params, Mapping):
                raise TypeError("RapidOCR 'params' must be a mapping")
            params = dict(supplied_params or {})
            params.update(options)
            params.setdefault(
                "EngineConfig.onnxruntime.intra_op_num_threads", int(ort_threads)
            )
            params.setdefault("EngineConfig.onnxruntime.inter_op_num_threads", 1)
            return rapidocr_class(config_path=config_path, params=params)

        # RapidOCR 1.x accepted engine options as constructor keyword arguments.
        if "Global.text_score" in options:
            text_score = options.pop("Global.text_score")
            if _supports_parameter(rapidocr_class, "text_score"):
                options.setdefault("text_score", text_score)
        if ort_threads != DEFAULT_ORT_THREADS and _supports_parameter(
            rapidocr_class, "intra_op_num_threads"
        ):
            options.setdefault("intra_op_num_threads", int(ort_threads))
        return rapidocr_class(**options)
    except OCRDependencyError:
        raise
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        raise OCRDependencyError(
            "RapidOCR could not load its runtime. Install a compatible 'onnxruntime' package."
        ) from exc
    except Exception as exc:
        raise OCREngineError(f"RapidOCR initialization failed: {exc}") from exc


def _configure_worker_threads(ort_threads: int) -> None:
    value = str(ort_threads)
    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[variable] = value


def initialize_ocr_worker(
    engine_kwargs: Mapping[str, Any] | None = None,
    ort_threads: int = DEFAULT_ORT_THREADS,
) -> None:
    """ProcessPool initializer: construct exactly one OCR engine per process."""

    global _WORKER_ENGINE
    _configure_worker_threads(ort_threads)
    _WORKER_ENGINE = create_ocr_engine(engine_kwargs, ort_threads=ort_threads)


def _plain_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    if hasattr(value, "tolist"):
        return _plain_value(value.tolist())
    if hasattr(value, "item"):
        try:
            return _plain_value(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _normalize_box(value: Any) -> list[Any]:
    plain = _plain_value(value)
    if plain is None:
        return []
    if isinstance(plain, list):
        if plain and all(isinstance(item, (int, float)) for item in plain):
            if len(plain) == 4:
                return [[plain[0], plain[1]], [plain[2], plain[3]]]
            if len(plain) % 2 == 0:
                return [plain[index : index + 2] for index in range(0, len(plain), 2)]
        return plain
    return []


def _canonical_item(text: Any, score: Any, box: Any) -> dict[str, Any] | None:
    if text is None:
        return None
    text_value = str(text).strip()
    if not text_value:
        return None
    return {
        "text": text_value,
        "confidence": _confidence(score),
        "box": _normalize_box(box),
    }


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _as_list(value: Any) -> list[Any]:
    plain = _plain_value(value)
    if plain is None:
        return []
    return plain if isinstance(plain, list) else [plain]


def _parallel_items(boxes: Any, texts: Any, scores: Any) -> list[dict[str, Any]]:
    text_values = _as_list(texts)
    if not text_values:
        return []
    box_values = _as_list(boxes)
    score_values = _as_list(scores)
    result: list[dict[str, Any]] = []
    for index, text in enumerate(text_values):
        box = box_values[index] if index < len(box_values) else []
        score = score_values[index] if index < len(score_values) else 0.0
        item = _canonical_item(text, score, box)
        if item is not None:
            result.append(item)
    return result


def _mapping_item(value: Mapping[str, Any]) -> dict[str, Any] | None:
    text = _first(value, ("text", "txt", "rec_text", "label"))
    if text is None:
        return None
    score = _first(value, ("confidence", "score", "rec_score", "probability"))
    box = _first(value, ("box", "points", "polygon", "dt_poly", "bbox"))
    return _canonical_item(text, 0.0 if score is None else score, box)


def _looks_like_box(value: Any) -> bool:
    plain = _plain_value(value)
    if not isinstance(plain, list) or not plain:
        return False
    if all(isinstance(item, (int, float)) for item in plain):
        return len(plain) >= 4
    return all(
        isinstance(point, list)
        and len(point) >= 2
        and all(isinstance(axis, (int, float)) for axis in point[:2])
        for point in plain
    )


def _sequence_item(value: Sequence[Any]) -> dict[str, Any] | None:
    if len(value) >= 3 and _looks_like_box(value[0]) and isinstance(value[1], str):
        return _canonical_item(value[1], value[2], value[0])
    if len(value) >= 3 and isinstance(value[0], str):
        return _canonical_item(value[0], value[1], value[2])
    if len(value) == 2 and isinstance(value[0], str):
        return _canonical_item(value[0], value[1], [])
    return None


def adapt_rapidocr_result(result: Any) -> list[dict[str, Any]]:
    """Convert old/new RapidOCR outputs into JSON-safe item dictionaries."""

    if result is None:
        return []

    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    if texts is None:
        texts = getattr(result, "texts", None)
    scores = getattr(result, "scores", None)
    if texts is not None:
        return _parallel_items(boxes, texts, scores)

    if isinstance(result, Mapping):
        direct_texts = _first(result, ("txts", "texts", "rec_texts"))
        if direct_texts is not None:
            return _parallel_items(
                _first(result, ("boxes", "dt_polys", "text_boxes", "rec_boxes")),
                direct_texts,
                _first(result, ("scores", "rec_scores", "confidences")),
            )

        item = _mapping_item(result)
        if item is not None:
            return [item]
        for key in ("result", "results", "data", "items", "res"):
            if result.get(key) is not None:
                return adapt_rapidocr_result(result[key])
        return []

    if hasattr(result, "to_json"):
        try:
            serialized = result.to_json()
        except Exception:
            serialized = None
        if serialized is not None and serialized is not result:
            return adapt_rapidocr_result(serialized)

    if isinstance(result, tuple) and len(result) == 2:
        first, second = result
        if isinstance(second, (int, float, Mapping)) or second is None:
            return adapt_rapidocr_result(first)

    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        if (
            len(result) == 3
            and isinstance(result[1], Sequence)
            and not isinstance(result[1], (str, bytes, bytearray))
            and all(isinstance(text, str) for text in result[1])
        ):
            return _parallel_items(result[0], result[1], result[2])

        items: list[dict[str, Any]] = []
        for value in result:
            if isinstance(value, Mapping):
                item = _mapping_item(value)
            elif isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                item = _sequence_item(value)
            else:
                item = None
            if item is not None:
                items.append(item)
        return items

    return []


def _box_geometry(box: Any) -> dict[str, float] | None:
    plain = _normalize_box(box)
    points: list[tuple[float, float]] = []
    for point in plain:
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            points.append((x, y))
    if not points:
        return None
    x_min = min(point[0] for point in points)
    x_max = max(point[0] for point in points)
    y_min = min(point[1] for point in points)
    y_max = max(point[1] for point in points)
    return {
        "x_min": x_min,
        "x_max": x_max,
        "y_min": y_min,
        "y_max": y_max,
        "x_center": (x_min + x_max) / 2,
        "y_center": (y_min + y_max) / 2,
        "width": max(0.0, x_max - x_min),
        "height": max(1e-6, y_max - y_min),
    }


def _same_visual_line(
    geometry: Mapping[str, float],
    line: Mapping[str, Any],
    tolerance: float,
) -> tuple[bool, float]:
    overlap = max(
        0.0,
        min(geometry["y_max"], line["y_max"])
        - max(geometry["y_min"], line["y_min"]),
    )
    minimum_height = min(geometry["height"], line["height"])
    overlap_ratio = overlap / minimum_height if minimum_height > 0 else 0.0
    center_distance = abs(geometry["y_center"] - line["y_center"])
    center_limit = tolerance * max(geometry["height"], line["height"])
    return overlap_ratio >= 0.30 or center_distance <= center_limit, center_distance


def _join_fragments(texts: Sequence[str], separator: str | None) -> str:
    if separator is not None:
        return separator.join(texts)
    result = ""
    for text in texts:
        if not result:
            result = text
            continue
        previous = result[-1]
        current = text[0]
        needs_space = previous.isascii() and current.isascii() and previous.isalnum() and current.isalnum()
        result += (" " if needs_space else "") + text
    return result


def _build_visual_line(
    entries: Sequence[dict[str, Any]],
    *,
    separator: str | None,
) -> dict[str, Any]:
    ordered = sorted(
        entries,
        key=lambda entry: (
            entry["geometry"]["x_min"] if entry["geometry"] else math.inf,
            entry["source_index"],
        ),
    )
    texts = [entry["item"]["text"] for entry in ordered]
    weights = [
        max(1.0, entry["geometry"]["width"]) if entry["geometry"] else 1.0
        for entry in ordered
    ]
    total_weight = sum(weights)
    confidence = sum(
        entry["item"]["confidence"] * weight
        for entry, weight in zip(ordered, weights)
    ) / total_weight
    geometries = [entry["geometry"] for entry in ordered if entry["geometry"]]
    if geometries:
        x_min = min(item["x_min"] for item in geometries)
        x_max = max(item["x_max"] for item in geometries)
        y_min = min(item["y_min"] for item in geometries)
        y_max = max(item["y_max"] for item in geometries)
        geometry_available = True
    else:
        x_min = x_max = y_min = y_max = 0.0
        geometry_available = False
    return {
        "text": _join_fragments(texts, separator),
        "confidence": float(confidence),
        "box": [[x_min, y_min], [x_max, y_min], [x_max, y_max], [x_min, y_max]],
        "x_min": x_min,
        "x_max": x_max,
        "y_min": y_min,
        "y_max": y_max,
        "x_center": (x_min + x_max) / 2,
        "y_center": (y_min + y_max) / 2,
        "width": max(0.0, x_max - x_min),
        "height": max(0.0, y_max - y_min),
        "geometry_available": geometry_available,
        "items": [entry["item"] for entry in ordered],
        "source_indices": [entry["source_index"] for entry in ordered],
    }


def group_visual_lines(
    items: Sequence[Mapping[str, Any]],
    *,
    line_tolerance: float = 0.65,
    separator: str | None = None,
) -> list[dict[str, Any]]:
    """Cluster OCR boxes by visual row and sort fragments from left to right."""

    if line_tolerance <= 0:
        raise ValueError("line_tolerance must be greater than zero")
    geometric: list[dict[str, Any]] = []
    without_geometry: list[dict[str, Any]] = []
    for source_index, raw_item in enumerate(items):
        item = {
            "text": str(raw_item.get("text", "")).strip(),
            "confidence": _confidence(raw_item.get("confidence", 0.0)),
            "box": _normalize_box(raw_item.get("box")),
        }
        if not item["text"]:
            continue
        entry = {
            "item": item,
            "source_index": source_index,
            "geometry": _box_geometry(item["box"]),
        }
        (geometric if entry["geometry"] else without_geometry).append(entry)

    geometric.sort(
        key=lambda entry: (
            entry["geometry"]["y_center"],
            entry["geometry"]["x_min"],
        )
    )
    clusters: list[dict[str, Any]] = []
    for entry in geometric:
        geometry = entry["geometry"]
        matches: list[tuple[float, int]] = []
        for index, cluster in enumerate(clusters):
            compatible, distance = _same_visual_line(geometry, cluster, line_tolerance)
            if compatible:
                matches.append((distance, index))
        if matches:
            _, cluster_index = min(matches)
            cluster = clusters[cluster_index]
            cluster["entries"].append(entry)
            cluster["x_min"] = min(cluster["x_min"], geometry["x_min"])
            cluster["x_max"] = max(cluster["x_max"], geometry["x_max"])
            cluster["y_min"] = min(cluster["y_min"], geometry["y_min"])
            cluster["y_max"] = max(cluster["y_max"], geometry["y_max"])
            cluster["height"] = max(1e-6, cluster["y_max"] - cluster["y_min"])
            cluster["y_center"] = (cluster["y_min"] + cluster["y_max"]) / 2
        else:
            clusters.append(
                {
                    "entries": [entry],
                    "x_min": geometry["x_min"],
                    "x_max": geometry["x_max"],
                    "y_min": geometry["y_min"],
                    "y_max": geometry["y_max"],
                    "height": geometry["height"],
                    "y_center": geometry["y_center"],
                }
            )

    result = [
        _build_visual_line(cluster["entries"], separator=separator) for cluster in clusters
    ]
    if without_geometry:
        result.append(_build_visual_line(without_geometry, separator=separator))
    return sorted(
        result,
        key=lambda line: (
            0 if line["geometry_available"] else 1,
            line["y_center"],
            line["x_min"],
        ),
    )


def _band_overlap(
    line: Mapping[str, Any],
    active_band: Sequence[float] | None,
    frame_height: float | None,
) -> float:
    if active_band is None or len(active_band) != 2 or not line.get("geometry_available"):
        return 0.0
    top, bottom = float(active_band[0]), float(active_band[1])
    if bottom <= top:
        raise ValueError("active_band bottom must be greater than top")
    if frame_height and 0 <= top <= 1 and 0 <= bottom <= 1:
        top *= frame_height
        bottom *= frame_height
    overlap = max(0.0, min(float(line["y_max"]), bottom) - max(float(line["y_min"]), top))
    height = max(1e-6, float(line["height"]))
    return overlap / height


def select_active_row(
    lines: Sequence[Mapping[str, Any]],
    active_row: str = "auto",
    *,
    active_band: Sequence[float] | None = None,
    frame_height: float | None = None,
) -> dict[str, Any] | None:
    """Select one visual subtitle row without joining multiple rows."""

    if active_row not in {"auto", "top", "bottom"}:
        raise ValueError("active_row must be one of: auto, top, bottom")
    if not lines:
        return None
    ordered = sorted(
        (dict(line) for line in lines),
        key=lambda line: (
            0 if line.get("geometry_available") else 1,
            float(line.get("y_center", 0.0)),
        ),
    )
    if active_row == "top":
        return ordered[0]
    if active_row == "bottom":
        geometric = [line for line in ordered if line.get("geometry_available")]
        return (geometric or ordered)[-1]

    def score(line: Mapping[str, Any]) -> tuple[float, float, float, int]:
        overlap = _band_overlap(line, active_band, frame_height)
        width = max(1.0, float(line.get("width", 0.0)))
        confidence = max(0.0, float(line.get("confidence", 0.0)))
        text_length = len(str(line.get("text", "")))
        return overlap, width * (0.5 + confidence / 2), confidence, text_length

    return max(ordered, key=score)


def process_ocr_output(
    result: Any,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    active_row: str = "auto",
    active_band: Sequence[float] | None = None,
    frame_height: float | None = None,
    line_tolerance: float = 0.65,
) -> dict[str, Any]:
    """Adapt, filter, visually group, and select an OCR result."""

    if not 0 <= confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be between 0 and 1")
    raw_items = adapt_rapidocr_result(result)
    items = [
        item for item in raw_items if item["confidence"] >= confidence_threshold
    ]
    rejected_items = [
        {"item": item, "reason": "low_confidence"}
        for item in raw_items
        if item["confidence"] < confidence_threshold
    ]
    visual_lines = group_visual_lines(items, line_tolerance=line_tolerance)
    selected = select_active_row(
        visual_lines,
        active_row,
        active_band=active_band,
        frame_height=frame_height,
    )
    if selected is not None:
        status = "ok"
    elif raw_items and not items:
        status = "low_confidence"
    else:
        status = "empty"
    return {
        "raw_items": raw_items,
        "items": items,
        "rejected_items": rejected_items,
        "visual_lines": visual_lines,
        "selected_line": selected,
        "box": selected["box"] if selected else None,
        "selected_box": selected["box"] if selected else None,
        "text": selected["text"] if selected else "",
        "confidence": float(selected["confidence"]) if selected else 0.0,
        "status": status,
        "error": None,
    }


def _invoke_engine(
    engine: Any,
    image_path: str,
    *,
    text_score: float | None = None,
) -> Any:
    if callable(engine):
        if text_score is not None and _accepts_keyword(engine, "text_score"):
            return engine(image_path, text_score=text_score)
        return engine(image_path)
    run = getattr(engine, "run", None)
    if callable(run):
        if text_score is not None and _accepts_keyword(run, "text_score"):
            return run(image_path, text_score=text_score)
        return run(image_path)
    raise TypeError("RapidOCR engine is neither callable nor exposes run()")


def _normalize_frame_spec(
    value: str | Path | Mapping[str, Any] | Sequence[Any],
    *,
    default_time: float,
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        path_value = value.get("path") or value.get("frame_path") or value.get("frame")
        if path_value is None:
            raise ValueError("Frame mapping must contain path, frame_path, or frame")
        path = Path(str(path_value)).expanduser()
        time_value = value.get("time", default_time)
        frame_name = str(value.get("frame") or path.name)
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray, Path))
        and len(value) == 2
    ):
        path = Path(str(value[0])).expanduser()
        time_value = value[1]
        frame_name = path.name
    else:
        path = Path(str(value)).expanduser()
        time_value = default_time
        frame_name = path.name
    try:
        timestamp = float(time_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid frame timestamp for '{path}': {time_value}") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"Frame timestamp must be finite and non-negative: {time_value}")
    return {"frame": frame_name, "path": str(path.resolve()), "time": timestamp}


def normalize_frame_specs(
    frames: Sequence[Any] | Mapping[str, Any],
    *,
    fps: int | float | str | Fraction = 4,
) -> list[dict[str, Any]]:
    """Accept paths, frame-index entries, or an extractor result dictionary."""

    if isinstance(frames, Mapping) and "frame_index" in frames:
        values: Iterable[Any] = frames["frame_index"]
    elif isinstance(frames, Mapping):
        values = [frames]
    else:
        values = frames
    try:
        fps_fraction = fps if isinstance(fps, Fraction) else Fraction(str(fps))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid fps value: {fps}") from exc
    if fps_fraction <= 0:
        raise ValueError("fps must be greater than zero")

    result: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        default_time = float(Fraction(index, 1) / fps_fraction)
        result.append(_normalize_frame_spec(value, default_time=default_time))
    return result


def _error_result(spec: Mapping[str, Any], exc: BaseException, stage: str = "ocr") -> dict[str, Any]:
    return {
        "frame": str(spec.get("frame", Path(str(spec.get("path", ""))).name)),
        "path": str(spec.get("path", "")),
        "time": float(spec.get("time", 0.0)),
        "raw_items": [],
        "items": [],
        "rejected_items": [],
        "visual_lines": [],
        "selected_line": None,
        "box": None,
        "selected_box": None,
        "text": "",
        "confidence": 0.0,
        "status": "error",
        "error": {"stage": stage, "type": type(exc).__name__, "message": str(exc)},
    }


def ocr_frame(
    frame: str | Path | Mapping[str, Any] | Sequence[Any],
    time: float | None = None,
    *,
    engine: Any = None,
    engine_kwargs: Mapping[str, Any] | None = None,
    ort_threads: int = DEFAULT_ORT_THREADS,
    text_score: float | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    active_row: str = "auto",
    active_band: Sequence[float] | None = None,
    frame_height: float | None = None,
    line_tolerance: float = 0.65,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """OCR one frame and always return a JSON-safe dictionary by default."""

    spec = _normalize_frame_spec(frame, default_time=0.0 if time is None else time)
    if time is not None:
        spec["time"] = float(time)
    try:
        path = Path(spec["path"])
        if not path.is_file():
            raise FileNotFoundError(f"Frame path does not exist or is not a file: {path}")
        active_engine = engine if engine is not None else create_ocr_engine(
            engine_kwargs, ort_threads=ort_threads
        )
        effective_text_score = _resolve_text_score(engine_kwargs, text_score)
        raw_result = _invoke_engine(
            active_engine,
            spec["path"],
            text_score=effective_text_score,
        )
        processed = process_ocr_output(
            raw_result,
            confidence_threshold=confidence_threshold,
            active_row=active_row,
            active_band=active_band,
            frame_height=frame_height,
            line_tolerance=line_tolerance,
        )
        return {"frame": spec["frame"], "path": spec["path"], "time": spec["time"], **processed}
    except Exception as exc:
        if raise_on_error:
            raise
        return _error_result(spec, exc)


def _worker_task(
    spec: Mapping[str, Any],
    confidence_threshold: float,
    active_row: str,
    active_band: Sequence[float] | None,
    frame_height: float | None,
    line_tolerance: float,
    text_score: float | None,
) -> dict[str, Any]:
    if _WORKER_ENGINE is None:
        return _error_result(spec, OCREngineError("OCR worker engine was not initialized"), "init")
    return ocr_frame(
        spec,
        engine=_WORKER_ENGINE,
        text_score=text_score,
        confidence_threshold=confidence_threshold,
        active_row=active_row,
        active_band=active_band,
        frame_height=frame_height,
        line_tolerance=line_tolerance,
    )


def ocr_all_frames(
    frames: Sequence[Any] | Mapping[str, Any],
    workers: int = DEFAULT_WORKERS,
    *,
    fps: int | float | str | Fraction = 4,
    engine: Any = None,
    engine_kwargs: Mapping[str, Any] | None = None,
    ort_threads: int = DEFAULT_ORT_THREADS,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    active_row: str = "auto",
    active_band: Sequence[float] | None = None,
    frame_height: float | None = None,
    line_tolerance: float = 0.65,
    fail_fast: bool = False,
    progress_callback: Callable[[int, int, Mapping[str, Any]], None] | None = None,
    cancellation_callback: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """OCR frames in order, recording individual failures instead of aborting."""

    if workers < 1:
        raise ValueError("workers must be at least one")
    if ort_threads < 1:
        raise ValueError("ort_threads must be at least one")
    text_score = _resolve_text_score(engine_kwargs, None)
    specs = normalize_frame_specs(frames, fps=fps)
    if not specs:
        return []
    if engine is not None and workers != 1:
        raise ValueError("A caller-provided engine can only be used with workers=1")

    results: list[dict[str, Any]] = []
    if workers == 1:
        if cancellation_callback:
            cancellation_callback()
        active_engine = engine if engine is not None else create_ocr_engine(
            engine_kwargs, ort_threads=ort_threads
        )
        for completed_count, spec in enumerate(specs, start=1):
            if cancellation_callback:
                cancellation_callback()
            result = ocr_frame(
                spec,
                engine=active_engine,
                text_score=text_score,
                confidence_threshold=confidence_threshold,
                active_row=active_row,
                active_band=active_band,
                frame_height=frame_height,
                line_tolerance=line_tolerance,
            )
            if fail_fast and result["status"] == "error":
                raise OCRFrameError(
                    f"OCR failed for frame '{result['frame']}': {result['error']['message']}"
                )
            results.append(result)
            if progress_callback:
                progress_callback(completed_count, len(specs), result)
        return results

    # Fail before spawning when the Python package itself is absent. Model/runtime
    # initialization still happens once inside each worker through the initializer.
    _load_rapidocr_class()
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=workers,
        initializer=initialize_ocr_worker,
        initargs=(dict(engine_kwargs or {}), ort_threads),
    )
    futures: list[concurrent.futures.Future[dict[str, Any]]] = []
    aborted = False
    try:
        for spec in specs:
            if cancellation_callback:
                cancellation_callback()
            futures.append(
                executor.submit(
                    _worker_task,
                    spec,
                    confidence_threshold,
                    active_row,
                    active_band,
                    frame_height,
                    line_tolerance,
                    text_score,
                )
            )
        for index, (spec, future) in enumerate(zip(specs, futures), start=1):
            if cancellation_callback is None:
                try:
                    result = future.result()
                except Exception as exc:
                    result = _error_result(spec, exc, "worker")
            else:
                while True:
                    cancellation_callback()
                    try:
                        result = future.result(timeout=FUTURE_POLL_INTERVAL_SECONDS)
                    except concurrent.futures.TimeoutError:
                        continue
                    except Exception as exc:
                        result = _error_result(spec, exc, "worker")
                    break
            if fail_fast and result["status"] == "error":
                for pending in futures[index:]:
                    pending.cancel()
                raise OCRFrameError(
                    f"OCR failed for frame '{result['frame']}': {result['error']['message']}"
                )
            results.append(result)
            if progress_callback:
                progress_callback(index, len(specs), result)
    except BaseException:
        aborted = True
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=aborted)
    return results


def failed_frames(results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return compact failure records suitable for a manifest or log file."""

    return [
        {
            "frame": str(result.get("frame", "")),
            "path": str(result.get("path", "")),
            "time": float(result.get("time", 0.0)),
            "error": _plain_value(result.get("error")),
        }
        for result in results
        if result.get("status") == "error"
    ]


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_ORT_THREADS",
    "DEFAULT_WORKERS",
    "OCRDependencyError",
    "OCREngineError",
    "OCRFrameError",
    "adapt_rapidocr_result",
    "create_ocr_engine",
    "failed_frames",
    "group_visual_lines",
    "initialize_ocr_worker",
    "normalize_frame_specs",
    "ocr_all_frames",
    "ocr_frame",
    "ocr_runtime_info",
    "process_ocr_output",
    "select_active_row",
]
