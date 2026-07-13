"""PyInstaller hook for the ONNX Runtime-only RapidOCR deployment."""

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


# RapidOCR resolves all three default models and both configuration documents
# relative to its installed package directory at runtime.
datas = collect_data_files(
    "rapidocr",
    include_py_files=False,
    includes=[
        "config.yaml",
        "default_models.yaml",
        "models/*.onnx",
    ],
)

# ocr_runtime_info() uses importlib.metadata for both distributions.
datas += copy_metadata("rapidocr")
datas += copy_metadata("onnxruntime")

hiddenimports = [
    "rapidocr.inference_engine.onnxruntime",
    "rapidocr.inference_engine.onnxruntime.main",
    "rapidocr.inference_engine.onnxruntime.provider_config",
]

excludedimports = [
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
