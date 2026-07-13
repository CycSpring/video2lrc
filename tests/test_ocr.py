from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import events
import ocr


BOX_TOP_LEFT = [[0, 10], [20, 10], [20, 20], [0, 20]]
BOX_TOP_RIGHT = [[25, 10], [50, 10], [50, 20], [25, 20]]
BOX_BOTTOM = [[5, 40], [80, 40], [80, 52], [5, 52]]


def test_adapt_current_output_object_and_numpy_values() -> None:
    np = pytest.importorskip("numpy")
    result = SimpleNamespace(
        boxes=np.asarray([BOX_TOP_LEFT, BOX_BOTTOM]),
        txts=("上行", "下行"),
        scores=(np.float32(0.91), np.float64(0.82)),
    )

    adapted = ocr.adapt_rapidocr_result(result)

    assert adapted == [
        {"text": "上行", "confidence": pytest.approx(0.91), "box": BOX_TOP_LEFT},
        {"text": "下行", "confidence": pytest.approx(0.82), "box": BOX_BOTTOM},
    ]
    json.dumps(adapted, ensure_ascii=False)


def test_adapt_legacy_tuple_and_mapping_formats() -> None:
    legacy = ([[BOX_TOP_LEFT, "第一句", 0.95], [BOX_BOTTOM, "第二句", 0.88]], 0.123)
    mapping = {
        "dt_polys": [BOX_TOP_LEFT],
        "rec_texts": ["映射结果"],
        "rec_scores": [0.77],
    }

    assert [item["text"] for item in ocr.adapt_rapidocr_result(legacy)] == [
        "第一句",
        "第二句",
    ]
    assert ocr.adapt_rapidocr_result(mapping)[0]["confidence"] == pytest.approx(0.77)


def test_group_visual_lines_sorts_y_then_x_and_keeps_rows_separate() -> None:
    items = [
        {"text": "下句", "confidence": 0.8, "box": BOX_BOTTOM},
        {"text": "世界", "confidence": 0.9, "box": BOX_TOP_RIGHT},
        {"text": "你好", "confidence": 0.95, "box": BOX_TOP_LEFT},
    ]

    lines = ocr.group_visual_lines(items)

    assert [line["text"] for line in lines] == ["你好世界", "下句"]
    assert lines[0]["source_indices"] == [2, 1]
    assert len(lines[0]["items"]) == 2
    assert len(lines[1]["items"]) == 1


def test_active_row_selection_honors_manual_and_band_modes() -> None:
    lines = ocr.group_visual_lines(
        [
            {"text": "上面", "confidence": 0.99, "box": BOX_TOP_LEFT},
            {"text": "下面活动行", "confidence": 0.70, "box": BOX_BOTTOM},
        ]
    )

    assert ocr.select_active_row(lines, "top")["text"] == "上面"
    assert ocr.select_active_row(lines, "bottom")["text"] == "下面活动行"
    assert ocr.select_active_row(lines, "auto")["text"] == "下面活动行"
    assert ocr.select_active_row(
        lines,
        "auto",
        active_band=(0.1, 0.4),
        frame_height=100,
    )["text"] == "上面"
    with pytest.raises(ValueError, match="active_row"):
        ocr.select_active_row(lines, "middle")


def test_process_output_filters_low_confidence_without_joining_two_rows() -> None:
    result = [
        [BOX_TOP_LEFT, "低分水印", 0.2],
        [BOX_TOP_RIGHT, "上行", 0.85],
        [BOX_BOTTOM, "下行歌词", 0.92],
    ]

    processed = ocr.process_ocr_output(
        result,
        confidence_threshold=0.5,
        active_row="bottom",
    )

    assert processed["text"] == "下行歌词"
    assert len(processed["visual_lines"]) == 2
    assert [item["text"] for item in processed["items"]] == ["上行", "下行歌词"]
    assert processed["rejected_items"][0]["item"]["text"] == "低分水印"
    assert processed["status"] == "ok"


def test_all_low_confidence_items_are_explicitly_recorded() -> None:
    processed = ocr.process_ocr_output(
        [[BOX_TOP_LEFT, "模糊", 0.3]], confidence_threshold=0.5
    )

    assert processed["status"] == "low_confidence"
    assert processed["text"] == ""
    assert processed["raw_items"][0]["text"] == "模糊"
    assert processed["items"] == []


def test_create_engine_uses_current_params_and_thread_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class CurrentRapidOCR:
        def __init__(self, config_path: str | None = None, params: dict[str, Any] | None = None):
            captured["config_path"] = config_path
            captured["params"] = params

    monkeypatch.setattr(ocr, "_load_rapidocr_class", lambda: CurrentRapidOCR)
    engine = ocr.create_ocr_engine(
        {"config_path": "custom.yaml", "Global.text_score": 0.6}, ort_threads=2
    )

    assert isinstance(engine, CurrentRapidOCR)
    assert captured["config_path"] == "custom.yaml"
    assert captured["params"]["Global.text_score"] == 0.6
    assert captured["params"]["EngineConfig.onnxruntime.intra_op_num_threads"] == 2
    assert captured["params"]["EngineConfig.onnxruntime.inter_op_num_threads"] == 1


def test_create_engine_supports_legacy_keyword_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class LegacyRapidOCR:
        def __init__(self, det_use_cuda: bool = False):
            captured["det_use_cuda"] = det_use_cuda

    monkeypatch.setattr(ocr, "_load_rapidocr_class", lambda: LegacyRapidOCR)
    engine = ocr.create_ocr_engine({"det_use_cuda": True})

    assert isinstance(engine, LegacyRapidOCR)
    assert captured == {"det_use_cuda": True}


def test_create_engine_maps_current_text_score_for_legacy_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class LegacyRapidOCR:
        def __init__(self, text_score: float = 0.5):
            captured["text_score"] = text_score

    monkeypatch.setattr(ocr, "_load_rapidocr_class", lambda: LegacyRapidOCR)
    ocr.create_ocr_engine({"Global.text_score": 0.1})

    assert captured == {"text_score": 0.1}


def test_missing_dependency_error_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> type[Any]:
        raise ocr.OCRDependencyError(
            "RapidOCR is unavailable. Install the 'rapidocr' and 'onnxruntime' packages."
        )

    monkeypatch.setattr(ocr, "_load_rapidocr_class", missing)
    with pytest.raises(ocr.OCRDependencyError) as error:
        ocr.create_ocr_engine()

    assert "rapidocr" in str(error.value)
    assert "onnxruntime" in str(error.value)


def test_ocr_frame_passes_unicode_path_and_returns_plain_dict(tmp_path: Path) -> None:
    frame = tmp_path / "中文 帧.png"
    frame.write_bytes(b"png")
    seen: list[str] = []

    class Engine:
        def __call__(self, path: str) -> list[list[Any]]:
            seen.append(path)
            return [[BOX_BOTTOM, "歌词", 0.93]]

    result = ocr.ocr_frame(frame, 1.25, engine=Engine())

    assert seen == [str(frame.resolve())]
    assert result["frame"] == frame.name
    assert result["time"] == 1.25
    assert result["text"] == "歌词"
    assert result["box"] == BOX_BOTTOM
    assert result["selected_box"] == BOX_BOTTOM
    assert result["status"] == "ok"
    json.dumps(result, ensure_ascii=False)


def test_ocr_frame_passes_text_score_to_early_rapidocr_call_api(tmp_path: Path) -> None:
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")
    seen: list[float] = []

    class EarlyRapidOCR:
        def __call__(self, _path: str, text_score: float = 0.5) -> list[list[Any]]:
            seen.append(text_score)
            return [[BOX_BOTTOM, "淡色歌词", 0.2]]

    result = ocr.ocr_frame(
        frame,
        engine=EarlyRapidOCR(),
        engine_kwargs={"Global.text_score": 0.1},
        confidence_threshold=0.1,
    )

    assert seen == [0.1]
    assert result["text"] == "淡色歌词"


def test_ocr_frame_records_failure_instead_of_aborting(tmp_path: Path) -> None:
    frame = tmp_path / "bad.png"
    frame.write_bytes(b"png")

    class BrokenEngine:
        def __call__(self, _path: str) -> object:
            raise RuntimeError("inference failed")

    result = ocr.ocr_frame(frame, 2.0, engine=BrokenEngine())

    assert result["status"] == "error"
    assert result["box"] is None
    assert result["selected_box"] is None
    assert result["error"] == {
        "stage": "ocr",
        "type": "RuntimeError",
        "message": "inference failed",
    }
    assert ocr.failed_frames([result]) == [
        {
            "frame": "bad.png",
            "path": str(frame.resolve()),
            "time": 2.0,
            "error": result["error"],
        }
    ]
    json.dumps(result)


def test_normalize_extractor_result_uses_index_times_and_fixed_defaults(tmp_path: Path) -> None:
    paths = [tmp_path / f"{number:06d}.png" for number in (1, 2, 3)]
    for path in paths:
        path.write_bytes(b"png")
    explicit = {
        "frame_index": [
            {"frame": paths[0].name, "path": str(paths[0]), "time": 7.5},
            {"frame": paths[1].name, "path": str(paths[1]), "time": 7.75},
        ]
    }

    assert [item["time"] for item in ocr.normalize_frame_specs(explicit)] == [7.5, 7.75]
    assert [item["time"] for item in ocr.normalize_frame_specs(paths, fps=4)] == [
        0.0,
        0.25,
        0.5,
    ]


def test_ocr_all_frames_single_worker_reuses_engine_and_records_failed_frame(
    tmp_path: Path,
) -> None:
    frames = [tmp_path / "000001.png", tmp_path / "000002.png"]
    for frame in frames:
        frame.write_bytes(b"png")

    class Engine:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, path: str) -> list[list[Any]]:
            self.calls += 1
            if path.endswith("000002.png"):
                raise ValueError("bad image")
            return [[BOX_BOTTOM, "成功", 0.9]]

    engine = Engine()
    progress: list[tuple[int, int, str]] = []
    results = ocr.ocr_all_frames(
        frames,
        workers=1,
        engine=engine,
        progress_callback=lambda done, total, result: progress.append(
            (done, total, result["status"])
        ),
    )

    assert engine.calls == 2
    assert [result["status"] for result in results] == ["ok", "error"]
    assert progress == [(1, 2, "ok"), (2, 2, "error")]


def test_process_pool_is_configured_with_windows_safe_initializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = [tmp_path / "000001.png", tmp_path / "000002.png"]
    for frame in frames:
        frame.write_bytes(b"png")
    captured: dict[str, Any] = {}

    class Engine:
        def __call__(self, _path: str) -> list[list[Any]]:
            return [[BOX_BOTTOM, "并行", 0.9]]

    class ImmediateFuture:
        def __init__(self, value: dict[str, Any]):
            self.value = value

        def result(self) -> dict[str, Any]:
            return self.value

        def cancel(self) -> bool:
            return True

    class FakeExecutor:
        def __init__(
            self,
            max_workers: int,
            initializer: Any,
            initargs: tuple[Any, ...],
        ) -> None:
            captured.update(
                max_workers=max_workers,
                initializer=initializer,
                initargs=initargs,
            )
            ocr._WORKER_ENGINE = Engine()

        def submit(self, function: Any, *args: Any) -> ImmediateFuture:
            return ImmediateFuture(function(*args))

        def shutdown(self, **kwargs: Any) -> None:
            captured["shutdown"] = kwargs

    monkeypatch.setattr(ocr, "_load_rapidocr_class", lambda: object)
    monkeypatch.setattr(ocr.concurrent.futures, "ProcessPoolExecutor", FakeExecutor)
    results = ocr.ocr_all_frames(
        frames,
        workers=2,
        engine_kwargs={"Global.text_score": 0.6},
        ort_threads=2,
    )

    assert captured["max_workers"] == 2
    assert captured["initializer"] is ocr.initialize_ocr_worker
    assert captured["initargs"] == ({"Global.text_score": 0.6}, 2)
    assert captured["shutdown"] == {"wait": True, "cancel_futures": False}
    assert [result["text"] for result in results] == ["并行", "并行"]


def test_parallel_cancellation_is_polled_and_workers_are_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = [tmp_path / "000001.png", tmp_path / "000002.png"]
    for frame in frames:
        frame.write_bytes(b"png")
    captured: dict[str, Any] = {"cancelled_futures": 0}

    class PendingFuture:
        def result(self, timeout: float | None = None) -> dict[str, Any]:
            assert timeout == ocr.FUTURE_POLL_INTERVAL_SECONDS
            raise TimeoutError

        def cancel(self) -> bool:
            captured["cancelled_futures"] += 1
            return True

    class FakeExecutor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def submit(self, _function: Any, *_args: Any) -> PendingFuture:
            return PendingFuture()

        def shutdown(self, **kwargs: Any) -> None:
            captured["shutdown"] = kwargs

    checks = 0

    def cancellation_callback() -> None:
        nonlocal checks
        checks += 1
        if checks >= 4:
            raise events.PipelineCancelled("stop")

    monkeypatch.setattr(ocr, "_load_rapidocr_class", lambda: object)
    monkeypatch.setattr(ocr.concurrent.futures, "ProcessPoolExecutor", FakeExecutor)

    with pytest.raises(events.PipelineCancelled, match="stop"):
        ocr.ocr_all_frames(
            frames,
            workers=2,
            cancellation_callback=cancellation_callback,
        )

    assert checks == 4
    assert captured["cancelled_futures"] == 2
    assert captured["shutdown"] == {"wait": True, "cancel_futures": True}


def test_initializer_creates_one_process_local_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engines: list[object] = []

    def fake_create(_kwargs: object, *, ort_threads: int) -> object:
        engine = {"threads": ort_threads}
        engines.append(engine)
        return engine

    monkeypatch.setattr(ocr, "create_ocr_engine", fake_create)
    monkeypatch.setattr(ocr, "_configure_worker_threads", lambda _threads: None)
    ocr.initialize_ocr_worker({"x": 1}, 2)

    assert len(engines) == 1
    assert ocr._WORKER_ENGINE is engines[0]
