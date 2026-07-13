# Video2LRC 原生桌面 UI Plan

## 目标

在不引入 Electron、Chromium 或内嵌 Web 服务的前提下，为现有 Python CLI 提供 Windows 桌面界面。UI 应保持空闲内存可控，OCR 运行期间不假死，并在任务退出后释放 RapidOCR、ONNX Runtime 和多进程模型内存。

## 架构

```text
PySide6-Essentials QWidget UI
            |
            | QProcess + V2LRC_EVENT JSONL
            v
      video2lrc CLI worker
            |
      ffmpeg / RapidOCR / ONNX
```

- GUI 只导入 QtCore、QtGui 和 QtWidgets，不导入 OCR、OpenCV 或 NumPy。
- 每次预览或生成任务使用独立 CLI 子进程。
- stdout 仅承载带 `V2LRC_EVENT ` 前缀的 JSONL 协议；stderr 显示为有上限的日志。
- 取消先写入标记文件，管线在阶段边界与 OCR 帧完成时协作退出；15 秒无响应时终止任务进程树。
- 冻结版缓存默认写入 `%LOCALAPPDATA%\Video2LRC\work`，不写安装目录。

## 界面范围

- 视频、输出路径与拖放。
- 底部比例或自定义 `x/y/w/h` ROI。
- ROI 拼图预览，不加载完整视频播放器。
- FPS、OCR 进程数、字幕样式、活动行和时间偏移。
- 置信度、文本相似度、确认帧、行间隔、缓存与严格模式。
- 进度、取消、日志、LRC 预览、复制和打开产物。
- 720x600 最小窗口和高 DPI 支持。

## 事件协议

每行格式为：

```text
V2LRC_EVENT {"type":"progress",...}
```

支持事件：

- `stage_started` / `stage_completed`
- `progress`
- `artifact`
- `completed`
- `cancelled`
- `failed`

每条事件包含 `job_id`、`seq` 和 UTC `timestamp`。GUI 忽略普通 stdout，协议行上限为 1 MiB。

## 发布

使用 PyInstaller `onedir` 双入口：

- `Video2LRC.exe`：无控制台原生 GUI。
- `video2lrc-cli.exe`：标准控制台 worker。
- `_internal`：共享 Qt、OCR、ONNX 和模型文件。

不采用 `onefile`，避免启动时解包和额外峰值内存。FFmpeg 默认使用系统 `PATH`，也可由构建参数显式捆绑。

## 验收

- 原有 CLI 行为和 JSON 输出兼容。
- GUI 参数以数组传入 QProcess，中文、空格与 `&` 路径可用。
- 预览、成功、失败、取消状态均可恢复，不留下运行锁。
- LRC 保持 UTF-8 无 BOM、LF 换行和视觉词组空格。
- 窗口在 1180x760 与 720x600 无重叠或文本遮挡。
- 源码测试、真实视频 GUI 冒烟、离屏启动和冻结版静态检查通过。
