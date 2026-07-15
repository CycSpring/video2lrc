# video2lrc

[English](README.md) | 简体中文

`video2lrc` 从音乐视频中提取烧录式歌词字幕，并生成行级 LRC 初稿。本项目面向仅使用 CPU 的 Windows 环境，目标是得到便于继续编辑的结果，而不是一次生成逐字精确的卡拉 OK 时间轴。

## 安装 Windows 发行版

请从 [GitHub Releases 页面](https://github.com/CycSpring/video2lrc/releases)下载当前版本：

- `Video2LRC-v0.1.0-windows-x64-setup.exe`：为当前 Windows 用户安装应用。
- `Video2LRC-v0.1.0-windows-x64-portable.zip`：功能相同的免安装版本。
- `SHA256SUMS.txt`：两个下载文件的 SHA-256 校验值。

发行包已内置 `ffmpeg` 和 `ffprobe`，普通用户无需另行配置。当前可执行文件尚未进行 Authenticode 代码签名，因此 Windows SmartScreen 可能显示“未知发布者”警告。

## 原生桌面界面

桌面应用通过 `PySide6-Essentials` 使用原生 Qt Widgets，不使用 Electron、Chromium、Qt WebEngine 或内嵌 Web 服务器。界面本身保持轻量，并通过独立 `QProcess` 启动现有 CLI，因此任务结束后 OCR 与 ONNX 占用的内存会随工作进程一起释放。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
.\.venv\Scripts\python.exe gui.py
```

桌面界面提供：

- 视频与输出文件选择，包括拖放操作。
- 底部裁剪和归一化自定义 ROI 模式，并可生成字幕区域预览拼图。
- 抽帧率、工作进程数、字幕样式、活动行、时间偏移、置信度与检测器参数。
- 一键恢复全部推荐处理参数，同时保留已选择的文件路径。
- 结构化阶段/OCR 进度、自动清除 ANSI 控制码并折叠常规空帧提示的受限日志、协作式取消和进程树强制终止后备方案。
- LRC 预览、复制/打开操作、持久化设置和缓存复用。

GUI 本身不会导入 RapidOCR、ONNX Runtime 或 OpenCV。工作 CLI 进程独占这些依赖，并通过带前缀的 JSONL 事件与窗口通信。

## 输出内容

- 纯 UTF-8 LRC 文件，每行采用 `[mm:ss.xx]歌词`，不包含元数据标签。
- 当多个视频帧持续观察到相同 OCR 文本块边界时，尽量保留词组之间的空格。
- `review.csv`，可在 Excel 中检查置信度、支持帧、时间戳和 QA 标记。
- `work/<run_id>/` 下的 JSON 阶段缓存，调整检测器或时间偏移时无需重复 OCR。
- 正式处理前可选生成覆盖多个时间点的 ROI 预览。

默认时间戳取字幕行首次稳定出现的采样时间。若字幕显示早于或晚于演唱，可使用 `--offset-ms` 进行全局补偿。

## 源码运行要求

- Windows，Python 3.11 或 3.12。
- `ffmpeg` 和 `ffprobe` 已加入 `PATH`。
- 使用 CPU 推理，无需 GPU 运行时。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
ffmpeg -version
```

`requirements.txt` 为源码开发保留相对宽松的直接依赖范围。发行构建始终安装 `requirements.lock.txt` 中固定的 Python 3.11 环境；`requirements-build.txt` 只是该锁文件的便捷入口。

## 先预览 ROI

默认 OCR 区域是视频底部 40%。处理完整歌曲前应先检查该区域：

```powershell
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --preview-roi
```

如果字幕不在该区域，请传入归一化的 `x,y,width,height` 坐标：

```powershell
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --roi 0.08,0.64,0.84,0.24 --preview-roi
```

## 生成 LRC 初稿

```powershell
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" -o "D:\Videos\song.lrc"
```

常见调整：

```powershell
# 复用 OCR，并将所有歌词行整体后移 350 毫秒。
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --resume --offset-ms 350 --force

# 提高采样密度，并保留抽取帧用于排查问题。
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --fps 8 --keep-workdir

# 双行字幕布局中，选择底部活动行。
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --active-row bottom

# 只预览检测结果，不写入最终 LRC。
.\.venv\Scripts\python.exe main.py "D:\Videos\song.mp4" --dry-run
```

运行 `python main.py --help` 可查看检测器、置信度、缓存和输出相关的全部参数。

## 构建 Windows 应用

项目支持的打包形式是 PyInstaller `onedir` 目录，两个启动器共享同一份依赖：

```text
dist\Video2LRC\
  Video2LRC.exe
  video2lrc-cli.exe
  _internal\
```

构建依赖系统 `PATH` 中 FFmpeg 的精简包：

```powershell
.\build.ps1
```

也可以显式内置 FFmpeg 与 FFprobe：

```powershell
.\build.ps1 -BundleFFmpeg -FfmpegPath "C:\Tools\ffmpeg.exe" -FfprobePath "C:\Tools\ffprobe.exe"
```

GUI 工作进程和直接运行的 `video2lrc-cli.exe` 都会从 `_internal\bin` 解析内置媒体工具，无需系统安装 FFmpeg。

完成内置 FFmpeg 的构建后，生成安装器和便携版资产：

```powershell
.\package-release.ps1 -Version 0.1.0 -FfmpegLicensePath "C:\path\to\ffmpeg\LICENSE"
```

发布打包脚本会将运行时许可文本复制到包中，使用 Inno Setup 和 `tar` 生成两个资产，并写入 `release\SHA256SUMS.txt`。第三方归属与源码链接见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

推送 `vX.Y.Z` 标签会运行[发行工作流](.github/workflows/release.yml)。该工作流下载经过固定 SHA-256 校验的 FFmpeg 与 Inno Setup 输入，重新执行完整构建，并将三个已验证资产发布到 GitHub Releases。

构建过程会验证 Python 3.11、安装 `requirements.lock.txt`、运行完整测试套件、收集 RapidOCR YAML 与 ONNX 模型，并检查 Qt、ONNX Runtime、可选媒体工具、可执行文件版本资源、冻结 CLI 启动和离屏 GUI 截图。脚本结束时会恢复它临时修改的全部进程环境变量。

发布前还应在干净的 Windows 机器上执行一次真实短视频 OCR。这个依赖媒体的检查不会作为所有构建的强制前提。生成的可执行文件包含产品与版本信息，但尚未进行代码签名。发布打包脚本会收集可用的第三方许可文本；依赖或 FFmpeg 构建发生变化时，维护者仍应重新审查这些说明。

界面架构和验收条件见 [UI_PLAN.md](UI_PLAN.md)。

## 缓存行为

每次运行都会保存 `manifest.json`，其中为抽帧、OCR、检测和写入分别记录缓存键：

- 仅修改 `--offset-ms` 时复用 `lines.json`。
- 修改检测器阈值时复用 `ocr_raw.json`。
- 修改 FPS 或 ROI 时，使抽帧及其全部下游阶段失效。
- 修改 OCR 包或模型版本时，使 OCR 及其全部下游阶段失效。

任务成功后，除非启用 `--keep-workdir`，抽取的 PNG 帧会被删除，JSON 与 CSV 诊断文件会保留。递归清理严格限制在所选工作根目录预留的 `<work_root>/<run_id>/frames` 中，自定义 `--workdir` 同样适用。

最终 LRC 必须位于所选工作根目录之外，因此 `--force` 无法覆盖输入视频或任何运行缓存。每次运行会持有独占 `.run.lock`；第二个进程若指向相同的续传缓存会直接失败，不会混合帧或清单。替换阶段产物前会先使对应缓存键失效，因此参数变更期间发生中断，也不会让旧缓存键指向新数据。

## 结果评估

`evaluator.py` 通过保持顺序的文本对齐比较生成结果与参考 LRC，并报告歌词行召回率/准确率、CER、估算的全局偏移、残差中位数/P90 时间误差以及人工编辑率。人工编辑率以参考歌词行数为分母；缺失行、匹配但错误的行和额外候选行都计为一次编辑，因此噪声严重时可能超过 100%。参考歌词必须来自相同的视频剪辑版本，时间指标才有意义。

```powershell
.\.venv\Scripts\python.exe evaluator.py reference.lrc candidate.lrc -o report.json
```

除非传入 `--force`，否则不会覆盖已有报告；报告路径也不能与任一输入 LRC 相同。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

单元测试使用固定 OCR 字典和模拟媒体命令。私有集成测试视频应放在 `local_testdata/` 中；该目录和生成的 LRC 文件不会进入 Git。

## MVP 限制

- 时间精度受采样率限制：4 FPS 代表最多约 250 毫秒的采样误差。
- 双行字幕需要收窄 ROI 或显式设置 `--active-row`；MVP 无法始终可靠推断当前演唱行。
- 即使相邻歌词行的 OCR 文本高度相似，也会保留稳定的新行；`similar_to_previous` 会标记歧义，避免静默合并可能真实存在的歌词。
- 输出是行级 LRC，不是逐字卡拉 OK 时间轴。
- 默认且受支持的路径是 `line` 模式；`typewriter` 和 `auto` 属于辅助模式。

完整设计、验收阈值和后续路线见 [PLAN.md](PLAN.md)。
