# 视频字幕 OCR -> LRC 生成器（MVP Plan）

> MVP 已完成实现；本文保留设计目标、关键决策与验收标准。

## 实施状态

- CLI、阶段缓存、ffprobe/ffmpeg 抽帧、RapidOCR、多进程适配、切行状态机、LRC/review 输出和 evaluator 均已实现。
- 项目使用 Python 3.11 独立 `.venv`，直接依赖已安装，完整版本记录在 `requirements.lock.txt`。
- 单元/编排测试与真实 ffmpeg + RapidOCR 冒烟均已通过；真实测试视频识别出 2/2 行，offset-only resume 未重复 OCR。
- 剩余验收项是用用户的实际 MV/参考 LRC 跑精度指标，以及实测 1/2/4/8 workers 的吞吐和内存。

## 目标

输入一个烧录了歌词字幕的音乐视频（如抖音风格 mp4），输出**行级 LRC 初稿**（纯歌词、无 ID 标签，格式 `[mm:ss.xx]歌词`）。

本项目不追求一次性完美卡点，目标是自动生成 80% 可用的 LRC，剩余少量偏差由人工微调。

“80% 可用”定义为：在与视频版本一致的参考 LRC 上，经一次全局 offset 校准后，至少 80% 的歌词行无需再改文字或时间戳；不把逐字高亮、自动听歌识词、GUI 校准器纳入 MVP。

## 环境前提

| 项 | 值 | 说明 |
|---|---|---|
| CPU | 现代 x64 多核处理器 | 全程 CPU 方案，不依赖 GPU |
| GPU | 可选 | MVP 不使用，后续需要批量加速再评估 |
| Python | 3.11.9 | 项目独立 `.venv`；不复用系统 site-packages |
| ffmpeg | 8.1.1 | `ffmpeg` 与 `ffprobe` 已通过真实抽帧测试 |
| OCR | `rapidocr 3.9.1` + `onnxruntime 1.27.0` | CPU 推理已通过真实冒烟 |
| 图像/匹配 | `opencv-python 5.0.0.93`、`rapidfuzz 3.14.5` | ROI 预览与文本聚类已验证 |

依赖策略：先用宽松版本完成一次安装和实测，再把实际可用版本写入 lock 文件或带版本号的 `requirements.txt`，避免后续环境漂移。旧包 `rapidocr-onnxruntime` 停留在 1.x 且不支持 Python 3.13，不作为新项目依赖。

## 产物格式基准

| 样本 | 来源 | 格式特征 |
|---|---|---|
| 纯 LRC 参考 | 本地测试夹具（不提交） | 纯 `[mm:ss.xx]歌词`，无 tag 头，时间戳精度 0.01s，行级 |
| 带标签 LRC 参考 | 本地测试夹具（不提交） | 带 `[ti:][ar:][al:]` 头，不采用 |

**本项目输出格式**：纯歌词、无标签、行级时间戳。

### 格式示例

```text
[00:00.00]第一行示例歌词
[00:02.50]第二行 示例歌词
[00:05.25]第三行示例歌词
```

## 关键洞察

抖音视频字幕即便逐字高亮（卡拉 OK 样式），OCR 通常仍能识别整行文本（灰色字符也可识别）。因此 MVP 先做**行级 LRC**，不做逐字 LRC（`<mm:ss.xx>` 内嵌标签）。

但“整行首帧出现时间”不一定等于真实开唱时间：字幕可能提前显示，也可能存在淡入、双行预告、上一句残留。MVP 的时间戳策略是：

```text
默认时间戳 = 该行首次稳定出现时间 + offset_ms
```

其中 `offset_ms` 可由用户全局调整。后续如果发现某类视频固定提前/滞后，可以用样本对比统计默认偏移。

---

## 项目结构

```text
<project_root>\
  main.py           # CLI 入口 + 参数解析
  pipeline.py       # 流程编排：抽帧 -> 裁剪 -> OCR -> 聚类切行 -> LRC
  extractor.py      # ffmpeg 抽帧 + 时间戳索引
  ocr.py            # RapidOCR + 多进程并行
  detector.py       # 字幕样式辅助检测 + 相似度聚类 + 行切换检测
  lrc_writer.py     # LRC 文件生成
  evaluator.py      # 与参考 LRC 对齐并计算文字/时间误差
  config.py         # 配置常量
  utils.py          # 时间戳格式化、文本规范化、路径处理
  requirements.txt
  requirements-dev.txt
  README.md
  .gitignore        # 忽略 work/、local_testdata/、生成的缓存与本地视频
  work/             # 运行缓存目录，不提交版本库
  local_testdata/   # 本地视频片段和参考 LRC，不提交版本库
  tests/
    test_detector.py
    test_utils.py
    fixtures/       # 小型 OCR JSON 固定样本，不存整段视频
```

运行目录统一放在项目内的 `<project_root>\work\<run_id>\`。默认路径从 `main.py` 所在项目根目录解析，不写死绝对路径，项目移动后仍可运行。`run_id` 由输入文件指纹生成；各阶段另存独立 cache key，避免下游参数变化导致无谓重算。

运行时生成：

- `frames/`：已经完成裁剪的抽帧缓存，不再额外生成 `cropped_frames/`
- `manifest.json`：输入文件指纹、依赖版本、ffmpeg 版本、全部参数和数据格式版本
- `ocr_raw.json`：每帧原始 OCR 框、文本、置信度、时间戳
- `ocr_clean.json`：每帧规范化后的候选字幕文本
- `lines.json`：聚类后的行、出现区间、代表文本、置信度和 QA 标记
- `review.csv`：方便人工快速检查时间戳、文字和低置信度行
- 最终 `.lrc`：写到用户指定位置或视频同目录

默认成功后保留 JSON/CSV 缓存、清理帧文件；`--keep-workdir` 保留全部中间产物。任何清理操作只允许删除本次创建且位于 `<project_root>\work\` 下的 run 目录，禁止递归删除项目根目录。`work/` 和 `local_testdata/` 必须加入 `.gitignore`。

---

## 流程编排（pipeline.py）

```text
1. probe_video(video) -> 视频流、时长、尺寸、旋转信息、起始时间
2. resolve_roi(video, crop_bottom_ratio 或 roi) -> 归一化字幕区域
3. extract_frames(video, roi, fps) -> 已裁剪 frames/ + frame_index
4. ocr_all_frames(frames, workers) -> OCRFrameResult[]
5. detect_style(ocr_results) -> style_hint ∈ {line, typewriter, uncertain}
6. detect_line_switches(ocr_results, style) -> LineEvent[]
7. apply_offset(lines, offset_ms)
8. write_review_files(lines) + write_lrc(lines, out_path)
```

MVP 默认 `--style line`，`--style auto` 只作为辅助能力。不要让样式自动检测成为项目能否跑通的关键路径。

每个阶段读取 `manifest.json` 判断缓存是否仍有效：

- extract key：输入指纹 + 旋转信息 + ROI + fps。
- OCR key：extract key + OCR 包/模型版本 + 预处理参数。
- detector key：OCR key + style + 相似度/确认帧等聚类参数。
- writer key：detector key + offset + 输出格式。

只改 `offset_ms` 或输出路径时，直接复用 `lines.json` 重写 LRC；只改 detector 阈值时复用 `ocr_raw.json`，不重新抽帧或 OCR。

---

## 模块设计

### extractor.py -- 抽帧

- 启动时先用 `ffprobe` 校验输入存在视频流，并读取时长、宽高、旋转信息、平均帧率和起始时间。
- ffmpeg 在一次滤镜链中完成时间归零、旋转后的 ROI 裁剪和抽帧，避免全帧与裁剪帧各存一份。
- 推荐滤镜思路：`setpts=PTS-STARTPTS,crop=...,fps={N}`；输出 `frames/%06d.png`，N 默认 4（抽样间隔 0.25s）。
- 因为先执行 `setpts=PTS-STARTPTS` 且输出固定 fps，MVP 时间戳可稳定定义为 `(帧序号 - 1) / fps`。实现时使用整数帧号或 `Fraction`/`Decimal`，避免浮点累计误差。
- `[mm:ss.xx]` 只是输出格式精度；4fps 的实际采样误差约为 0-250ms，不能宣称有 10ms 卡点精度。
- 单线程，IO 密集，ffmpeg 本身够快。
- 抽帧率可配置（2-8fps）。卡点要求高时用 6-8fps，速度优先时用 3-4fps。
- 所有 `subprocess` 调用传参数数组，不拼 shell 命令，确保中文、空格和特殊字符路径可用。

### cropper（可放在 extractor.py 或 pipeline.py）

- MVP 不做全帧 OCR，默认裁剪底部区域，降低水印、标题、贴纸和背景文字干扰。
- 默认：`crop_bottom_ratio = 0.40`，即保留画面底部 40%。
- CLI 支持 `--crop-bottom-ratio 0.4`，以及归一化坐标 `--roi x,y,w,h`（取值 0-1），避免分辨率变化后像素坐标失效。
- 提供 `--preview-roi`：从开头、中段和后段各取若干帧，画出 ROI 并生成预览拼图后退出。用户先确认区域，再执行完整 OCR。
- `ocr_raw.json` 中记录裁剪参数，便于复现。
- MVP 明确只保证“单个活动歌词行”场景。双行字幕必须通过更窄 ROI 或 `--active-row top|bottom` 指定活动行；若两行都被 OCR 后直接拼接，会产生提前卡点和错行，不能静默处理。

### ocr.py -- OCR（多进程并行）

- RapidOCR (`rapidocr`) + ONNX Runtime (`onnxruntime`)，CPU 推理。封装一层本项目自己的结果适配器，隔离 RapidOCR 版本间返回对象/API 的变化。
- Windows 使用 `ProcessPoolExecutor` 时，入口必须放在 `if __name__ == "__main__":` 下，并通过 worker initializer 每进程只初始化一次 OCR 引擎。
- 不默认启动 16 个进程。ONNX Runtime 自身会开线程，进程数过多会造成 `进程数 x ORT 线程数` 的过度竞争和模型内存复制。默认从 `workers=4` 起测，再比较 4/8 个 worker；若可配置，将每个 worker 的 ORT intra-op 线程限制为 1-2。
- 每个 worker 初始化时创建一个 RapidOCR 实例，避免反复初始化开销。
- 输入：裁剪后的帧路径列表。
- worker 返回纯 Python dict/list/number/string，不把 OCR 引擎对象或第三方结果对象跨进程传输。
- 输出：`OCRFrameResult`：

```json
{
  "frame": "000123.png",
  "time": 30.5,
  "raw_items": [
    {"text": "第一段 示例歌词", "confidence": 0.91, "box": [[...]]}
  ],
  "text": "第一段 示例歌词",
  "confidence": 0.91
}
```

- 文本选择策略：
  - 丢弃低置信度文本框。
  - OCR 框先按垂直中心聚成视觉行，再在行内按 x 坐标排序拼接，不能依赖 OCR 返回顺序。
  - 优先选择与指定活动行带重叠最大、宽度较大、置信度较高的视觉行。
  - 多行字幕保留独立行结构；禁止直接把两行拼成一句歌词。
  - 对低置信度帧可选执行一次轻量预处理重试（对比度增强或灰度化），但不在所有帧上同时跑多套预处理。

### utils.py -- 文本规范化

用于判断“是否同一句”的文本应先规范化，但最终输出仍尽量使用原始代表文本。

规范化建议：

- 去首尾空白。
- 合并连续空格。
- 使用 Unicode NFKC 统一全角/半角。
- 去除明确的装饰符号和 OCR 噪声符号，但不能无条件删除 `&`、英文撇号等可能属于歌词的字符。
- 判断相似度时可去掉空格和常见标点。
- 保留 `raw_text`、`display_text`、`match_text` 三个层次：原始识别、最终输出、仅用于匹配的规范化文本互不覆盖。
- LRC 输出编码固定为 UTF-8 无 BOM，换行固定为 `\n`，与项目格式约定一致。
- `review.csv` 使用 UTF-8 BOM（`utf-8-sig`），确保 Windows Excel 直接打开中文不乱码。

### detector.py -- 样式辅助检测 + 切换检测（核心）

#### 阶段 1：样式辅助检测（detect_style）

- 从已有 OCR 结果中抽取非空帧，不额外重复 OCR。
- 分析相邻非空帧文本关系：
  - 高相似度：同一句。
  - 后帧是前帧前缀扩展：可能是打字机。
  - 大幅变化：可能是新行。
- 输出只作为 `style_hint` 和日志。
- 默认仍按 `line` 路径执行，用户可通过 `--style typewriter` 强制打字机模式。

#### 阶段 2：整行模式（默认）

不能用“文本变化 = 新行”，必须使用带 pending 状态的时序聚类，避免单帧 OCR 抖动直接关闭当前行。

核心规则：

```text
for each non-empty OCR frame:
  normalized = normalize(text)
  if current_cluster is empty:
    append to pending candidate
    pending 达到 confirm_frames 后，建立 current_cluster
  else if same_line(normalized, current_cluster.representative):
    append to current_cluster
    clear pending candidate
  else:
    append to pending candidate
    if pending candidate 自身连续一致且达到 confirm_frames:
      close current_cluster
      start new cluster，起点回溯到 pending 的第一帧
    elif current text 再次出现:
      discard pending candidate，将其记为 OCR 抖动
```

`same_line()` 不能只用固定的 `rapidfuzz ratio >= 90`：

- 长句可用归一化编辑距离或 ratio。
- 短句对一个错字非常敏感，也容易把不同歌词误并。长度小于 5 时应提高约束，结合时间连续性、OCR 框位置和置信度；不能仅凭模糊相似度合并。
- typewriter 模式额外允许前缀包含关系。
- 相邻两行即使文字相似，只要新文本已稳定出现，也应保留为新行。

聚类结束后，每个 cluster 输出一行：

- `start_time`：该簇候选序列第一帧时间，而不是达到 `confirm_frames` 的确认帧时间，避免人为增加一个采样间隔。
- `end_time`：该簇最后出现时间。
- `text`：簇内代表文本采用 medoid 思路：选择与其他候选总编辑距离最小的原始文本，再用平均置信度和长度打破平局；不要依赖“完全相同字符串的多数票”。
- `confidence`：簇内平均或中位置信度。
- `support_frames`：支持该行的帧数。

确认新行时使用稳定帧：

- `confirm_frames = 2`：至少连续 2 帧相似文本才确认。
- `min_line_gap_ms = 800`：只产生 `short_gap` QA 标记，不得仅凭时间过近自动合并，快歌确实可能存在短行。
- 空帧允许短暂存在：连续 1-2 个空帧不立即关闭当前行，避免字幕淡入淡出造成断裂。
- 单个高置信度帧默认仍不成行，但写入 `rejected_candidates`，避免极短字幕无声丢失且无法追查。

#### 阶段 3：打字机模式（可选）

用于 OCR 文本随时间逐字增长的字幕：

- 如果后帧是前帧的前缀扩展，归入同一行。
- 行起点取该行第一个非空片段的稳定出现时间。
- 行文本取该行最后几个稳定帧中最长且置信度较高的文本。
- 当文本不再保持前缀关系且相似度明显下降时，关闭当前行并开启新行。

#### 通用后处理

- 跳过空识别帧（间奏/无字幕），但记录空白区间。
- 跳过低置信度帧（< `confidence_threshold`），不参与切行但记日志。
- 合并重复副歌时不要按全局文本去重，只按连续时间区间去重。相同歌词在不同时间重复出现必须保留。
- `lines.json` 中保留 `start_time_raw`、`start_time_offset`、`end_time`、`text_candidates`、`support_frames`、`qa_flags` 和被拒候选，便于人工审查。
- 输入结束时必须 flush 当前 cluster 和 pending candidate，避免漏掉最后一句。

### lrc_writer.py -- LRC 生成

- 时间戳格式 `[mm:ss.xx]`（精度 0.01s）。
- 纯歌词，无 ID 标签。
- 按时间戳升序输出。
- 行时间戳 = `start_time_raw + offset_ms`，下限裁到 0。
- 相同或倒序时间戳不自动偷偷修正，写入 QA 标记并在严格模式下报错；四舍五入到百分之一秒后再检查顺序。
- 默认拒绝覆盖已有输出文件，只有 `--force` 才允许覆盖。
- 可选 `--dry-run` 预览前 N 行；仅修改 offset 时从缓存重写 LRC。
- 同步生成 `review.csv`：`line_no,start_raw,start_final,end,text,confidence,support_frames,qa_flags`。

---

## 配置项（config.py）

| 项 | 默认 | 说明 |
|---|---|---|
| fps | 4 | 抽帧率，精度约 0.25s |
| workers | 4 | OCR 进程数；实测 4/8 后再调整，避免 ORT 线程过度竞争 |
| crop_bottom_ratio | 0.40 | 默认裁剪画面底部 40% |
| confidence_threshold | 0.5 | OCR 置信度下限 |
| raw_ocr_text_score | 0.10 | RapidOCR 引擎原始召回下限；始终不高于用户阈值，低分项交给后处理记录/过滤 |
| same_threshold | 90 | 长句相似度初始值；短句走更严格的长度感知规则 |
| switch_confirm_frames | 2 | 确认行切换所需连续帧数 |
| min_line_gap_ms | 800 | 仅用于 QA 标记，不自动合并 |
| max_blank_frames_inside_line | 2 | 行内允许的短暂空帧数 |
| offset_ms | 0 | 全局时间偏移 |
| style | line | `line`、`typewriter`、`auto` |
| active_row | auto | `auto`、`top`、`bottom`；双行字幕建议手动指定 |

---

## CLI 设计（main.py）

```text
python main.py <video_path> [-o output.lrc] [--fps 4] [--workers 4] [--keep-workdir]

参数：
  video_path                    输入视频路径（必填）
  -o, --output                  输出 LRC 路径（默认与视频同目录同名 .lrc）
  --fps                         抽帧率（默认 4）
  --workers                     OCR 并行进程数（默认 4）
  --workdir                     运行目录根路径（默认 <project_root>\work）
  --keep-workdir                成功后保留帧和全部中间产物
  --resume                      复用参数匹配的抽帧/OCR/聚类缓存
  --style                       强制字幕样式：line|typewriter|auto（默认 line）
  --crop-bottom-ratio           裁剪底部比例（默认 0.40）
  --roi                         归一化字幕区域：x,y,w,h，取值 0-1
  --preview-roi                 生成多时点 ROI 预览后退出
  --active-row                  活动字幕行：auto|top|bottom
  --offset-ms                   全局时间偏移，正数延后，负数提前
  --same-threshold              同一句相似度阈值（默认 90）
  --min-line-gap-ms             最小行间隔（默认 800）
  --dry-run                     仅预览识别结果，不写最终 LRC
  --force                       允许覆盖已有输出文件
```

`--crop-bottom-ratio` 和 `--roi` 互斥；`--resume` 只有在输入指纹、ROI、fps、OCR 模型和预处理参数一致时才复用对应缓存。

---

## 错误处理与可观测性

- 中间产物写入唯一的 `<project_root>\work\<run_id>\`；JSON 原子写入（先写同目录临时文件再 replace），避免中断后留下半份缓存。
- 日志记录：视频信息、fps、ROI、OCR/ORT/ffmpeg 版本、OCR 进度、低置信度帧、空白区间、样式检测结果、疑似误切行和各阶段耗时。
- OCR 失败帧跳过并记录，不中断流程。
- 若有效 OCR 帧过少，提示用户调整 `--crop-bottom-ratio`、`--fps` 或换 PaddleOCR。
- 对每行输出 `support_frames` 和 `confidence`，方便后续 UI 或人工检查优先处理低置信度行。
- `Ctrl+C` 时停止提交新任务，安全关闭 worker；默认保留失败 run 的调试数据。
- 输入缺少视频流、ffmpeg/ffprobe 不可用、ROI 越界、输出已存在等情况在抽帧前快速失败。

## 性能预期（现代多核 CPU，4 分钟歌曲）

| 环节 | 耗时 |
|---|---|
| 抽帧 4fps -> 960 帧 | ffmpeg 数秒到几十秒，取决于编码和磁盘 |
| OCR 960 张裁剪图，4-8 进程 | 预计几十秒级，需以实测为准 |
| 聚类切行 + 写 LRC | 秒级 |
| **总计一首歌** | 目标 1 分钟左右，先以稳定性优先 |

---

## 验收与评估

不能只凭“听起来差不多”验收。使用与视频版本一致的参考 LRC，先按歌词文本做顺序对齐，再评估匹配行的时间差。

建议指标：

- `line_recall`：参考歌词行中成功识别并对齐的比例。
- `line_precision`：输出行中不是水印、标题或误切行的比例。
- `CER`：匹配歌词行的中文字符错误率。
- `raw_time_error`：未调 offset 的时间误差。
- `residual_time_error`：用匹配行时间差的中位数估算全局 offset 后，剩余误差的 median 和 P90。
- `manual_edit_rate`：需要人工修改文字或时间戳的歌词行占比，这是最终产品指标。

MVP 初始验收线（可按首批样本调整）：

- `line_recall >= 90%`
- `line_precision >= 95%`
- 匹配行 `CER <= 8%`
- 全局 offset 后，时间绝对误差 median `<= 0.35s`、P90 `<= 0.80s`
- `manual_edit_rate <= 20%`

参考 LRC 与视频若不是同一剪辑/混音版本，不用于时间指标，只用于文字检查。

## 测试策略

- 单元测试：时间戳格式、Unicode 规范化、短句/长句 `same_line()`、LRC 编码、负 offset 裁零。
- 状态机固定样本：单帧错字、短暂空帧、稳定换行、极短字幕、最后一句 flush、相同副歌重复出现。
- 双行字幕固定样本：验证不会把上下两行直接拼接，并验证 `active_row`。
- 集成测试：使用 15-30 秒短视频片段跑通 ffprobe -> 抽帧 -> OCR -> LRC；测试素材放 `<project_root>\local_testdata\`，并加入 `.gitignore`。
- 回归测试：把人工确认过的 `ocr_raw.json -> lines.json` 作为 golden fixture，算法改动后比较行数、文本和时间戳。

---

## 风险与待确认（真实素材验收后定）

1. **字幕出现时间不等于开唱时间**：用 `offset_ms` 解决整体偏移；个别行保留人工修正。
2. **字幕区域**：默认裁剪底部 40%，但抖音视频可能有双行、上方标题或动态字幕，需要先跑 `--preview-roi` 并人工调 `--roi`。
3. **OCR 准确性**：中文歌词 + 背景干扰可能导致错字。先靠裁剪、置信度、相似度聚类缓解；若效果不佳，可换 PaddleOCR 原版。
4. **样式检测不稳定**：MVP 默认 line，不依赖 auto；auto 只输出提示。
5. **时间精度**：固定 fps + 时间归零足够 MVP，但实际精度受抽帧率限制。若仍不够，再提高 fps 或增加局部二次抽帧，不先引入音频对齐。
6. **逐字卡点**：当前产出行级 LRC。若未来想要逐字时间戳，需额外检测高亮边界或引入音频对齐，作为 v2 功能。
7. **双行字幕**：无法仅靠整块 OCR 自动判断哪一行正在唱。MVP 要求 ROI/active-row 明确；全自动活动行检测放到后续版本。
8. **并发性能**：ONNX Runtime 线程模型可能使更多 worker 反而更慢，必须用同一视频片段实测 1/2/4/8 worker 后决定默认值。

## MVP 之后的优先增强

1. **局部二次抽帧**：第一遍 4fps 找到候选换行点，再只对每个切换点前后约 0.5 秒以 12-20fps 重抽并 OCR，把时间误差从约 250ms 降到 50-80ms，而不是全片提高 fps。
2. **字幕变化预筛选**：对 ROI 做低成本图像差异检测，跳过明显重复帧；必须保守设计，不能因背景运动或渐变漏掉换行。
3. **活动行检测**：根据高亮颜色、文本框位置和行滚动规律处理双行字幕。
4. **人工校准界面**：读取 `review.csv/lines.json`，播放视频并支持行级前移、后移和改字。

---

## 真实素材验收步骤

1. 对实际 MV 运行 `--preview-roi`，确认字幕区域和活动行。
2. 用 15-30 秒片段跑 4fps，查看 `ocr_raw.json` 前 20 条；如混入水印/标题，调整 ROI。
3. 实测 1/2/4/8 workers，记录吞吐和内存，再决定是否调整默认值。
4. 跑完整视频，生成 LRC、review.csv 和评估报告；根据参考 LRC 估算全局 offset。
5. 只改 `--offset-ms` 并从缓存重写 LRC，确认不会重新 OCR。
6. 记录错字、漏行、误切和双行字幕问题，再决定是否上 PaddleOCR 或手动校准界面。

---

## requirements.txt

```text
rapidocr
onnxruntime
opencv-python
numpy<3
rapidfuzz
```

`requirements-dev.txt`：

```text
-r requirements.txt
pytest
```

注：ffmpeg 为系统级二进制，不放入 requirements.txt；已验证 Python 环境的完整版本固定在 `requirements.lock.txt`。
