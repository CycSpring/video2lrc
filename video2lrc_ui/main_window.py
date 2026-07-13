"""Main Qt Widgets window for the native video2lrc desktop application."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QSize, QStandardPaths, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QGuiApplication,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config import (
    DEFAULT_ACTIVE_ROW,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_CROP_BOTTOM_RATIO,
    DEFAULT_FPS,
    DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE,
    DEFAULT_MIN_LINE_GAP_MS,
    DEFAULT_OFFSET_MS,
    DEFAULT_SAME_THRESHOLD,
    DEFAULT_STYLE,
    DEFAULT_SWITCH_CONFIRM_FRAMES,
    DEFAULT_WORKERS,
)

from .models import ROI, JobSpec
from .process_runner import ACTIVE_STATES, ProcessRunner, RunnerState
from .widgets import AspectPreviewLabel, CollapsibleSection, PathField, StatusBanner


VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.avi *.webm);;所有文件 (*)"
VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".avi", ".webm"})
STAGE_LABELS = {
    "prepare": "检查视频",
    "preview": "生成区域预览",
    "extract": "提取字幕帧",
    "ocr": "识别字幕",
    "detector": "整理歌词行",
    "writer": "写入 LRC",
}
STAGE_RANGES = {
    "prepare": (0, 8),
    "preview": (8, 100),
    "extract": (8, 30),
    "ocr": (30, 90),
    "detector": (90, 96),
    "writer": (96, 100),
}
DEFAULT_BOTTOM_RATIO_PERCENT = round(DEFAULT_CROP_BOTTOM_RATIO * 100)
DEFAULT_CUSTOM_ROI_PERCENT = {
    "x": 18.0,
    "y": 76.0,
    "width": 78.0,
    "height": 22.0,
}
DEFAULT_RESUME = True


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    label.setMinimumHeight(24)
    return label


def _form() -> QFormLayout:
    layout = QFormLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setHorizontalSpacing(10)
    layout.setVerticalSpacing(8)
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return layout


def _tool_button(
    parent: QWidget,
    icon: QStyle.StandardPixmap,
    tooltip: str,
) -> QToolButton:
    button = QToolButton(parent)
    button.setFixedSize(32, 32)
    button.setIcon(parent.style().standardIcon(icon))
    button.setIconSize(QSize(17, 17))
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    return button


class MainWindow(QMainWindow):
    """A responsive native desktop shell around the existing CLI pipeline."""

    def __init__(
        self,
        video_path: str | Path | None = None,
        *,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Video2LRC")
        self.setMinimumSize(720, 600)
        self.resize(1180, 760)
        self.setAcceptDrops(True)

        self._settings = settings or QSettings("Video2LRC", "Video2LRC")
        self._runner = ProcessRunner(self)
        self._current_mode: str | None = None
        self._current_result: dict[str, Any] | None = None
        self._output_path: Path | None = None
        self._review_path: Path | None = None
        self._run_dir: Path | None = None
        self._close_after_finish = False
        self._restoring = False
        self._last_video_path: Path | None = None

        self._build_ui()
        self._connect_signals()
        self._restore_settings()
        if video_path is not None:
            self.set_video_path(video_path)
        self._update_roi_mode()
        self._validate_form()

    @property
    def runner(self) -> ProcessRunner:
        return self._runner

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        root.addWidget(self._build_header())

        self.banner = StatusBanner()
        self.banner.setVisible(False)
        banner_wrap = QWidget(self)
        banner_layout = QVBoxLayout(banner_wrap)
        banner_layout.setContentsMargins(14, 8, 14, 0)
        banner_layout.addWidget(self.banner)
        root.addWidget(banner_wrap)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self._build_settings_pane())
        self.main_splitter.addWidget(self._build_result_pane())
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([350, 810])
        root.addWidget(self.main_splitter, 1)
        root.addWidget(self._build_run_bar())

        self.statusBar().clearMessage()
        self.statusBar().setSizeGripEnabled(True)

    def _build_header(self) -> QWidget:
        header = QFrame(self)
        header.setObjectName("headerBar")
        header.setFixedHeight(48)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(9)

        icon = QLabel(header)
        icon.setObjectName("brandIcon")
        brand_icon = QGuiApplication.windowIcon()
        if brand_icon.isNull():
            brand_icon = self.style().standardIcon(
                QStyle.StandardPixmap.SP_FileDialogDetailedView
            )
        icon.setPixmap(brand_icon.pixmap(24, 24))
        icon.setFixedSize(24, 24)
        title = QLabel("Video2LRC", header)
        font = title.font()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        subtitle = QLabel("原生 Qt 桌面版", header)
        subtitle.setProperty("secondary", True)

        self.open_run_dir_button = _tool_button(
            header,
            QStyle.StandardPixmap.SP_DirOpenIcon,
            "打开运行目录",
        )
        self.open_run_dir_button.setEnabled(False)

        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(self.open_run_dir_button)
        return header

    def _build_settings_pane(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(315)
        scroll.setMaximumWidth(430)

        panel = QWidget(scroll)
        panel.setObjectName("settingsPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)
        self.settings_panel = panel

        settings_header = QHBoxLayout()
        settings_header.setContentsMargins(0, 0, 0, 0)
        settings_header.addWidget(_section_label("文件"))
        settings_header.addStretch(1)
        self.reset_parameters_button = QPushButton("恢复默认参数", panel)
        self.reset_parameters_button.setObjectName("resetParametersButton")
        self.reset_parameters_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogResetButton)
        )
        self.reset_parameters_button.setToolTip("恢复初始推荐参数，保留视频和输出路径")
        self.reset_parameters_button.setAccessibleName("恢复默认参数")
        self.reset_parameters_button.setFixedHeight(30)
        settings_header.addWidget(self.reset_parameters_button)
        layout.addLayout(settings_header)
        layout.addWidget(QLabel("视频"))
        self.video_field = PathField(
            panel,
            placeholder="选择视频文件",
            dialog_caption="选择音乐视频",
            dialog_filter=VIDEO_FILTER,
        )
        layout.addWidget(self.video_field)
        layout.addWidget(QLabel("输出 LRC"))
        self.output_field = PathField(
            panel,
            placeholder="输出 LRC 路径",
            dialog_caption="选择输出 LRC",
            dialog_filter="LRC 歌词 (*.lrc);;所有文件 (*)",
            save_file=True,
        )
        layout.addWidget(self.output_field)

        layout.addWidget(_section_label("字幕区域"))
        roi_form = _form()
        self.roi_mode = QComboBox(panel)
        self.roi_mode.addItem("底部区域", "bottom")
        self.roi_mode.addItem("自定义区域", "custom")
        self.roi_mode.setAccessibleName("字幕区域模式")
        roi_form.addRow("模式", self.roi_mode)

        self.bottom_ratio = QSpinBox(panel)
        self.bottom_ratio.setRange(10, 90)
        self.bottom_ratio.setValue(DEFAULT_BOTTOM_RATIO_PERCENT)
        self.bottom_ratio.setSuffix(" %")
        self.bottom_ratio.setAccessibleName("底部区域比例")
        roi_form.addRow("底部比例", self.bottom_ratio)

        self.active_row = QComboBox(panel)
        for label, value in (("自动", "auto"), ("上行", "top"), ("下行", "bottom")):
            self.active_row.addItem(label, value)
        self.active_row.setCurrentIndex(self.active_row.findData(DEFAULT_ACTIVE_ROW))
        self.active_row.setAccessibleName("活动字幕行")
        roi_form.addRow("活动行", self.active_row)
        layout.addLayout(roi_form)

        self.custom_roi_widget = QWidget(panel)
        custom_form = _form()
        self.custom_roi_widget.setLayout(custom_form)
        self.roi_spins: dict[str, QDoubleSpinBox] = {}
        labels = {"x": "X", "y": "Y", "width": "宽", "height": "高"}
        for key in ("x", "y", "width", "height"):
            spin = QDoubleSpinBox(self.custom_roi_widget)
            spin.setRange(0.0 if key in {"x", "y"} else 1.0, 100.0)
            spin.setDecimals(1)
            spin.setSingleStep(1.0)
            spin.setSuffix(" %")
            spin.setValue(DEFAULT_CUSTOM_ROI_PERCENT[key])
            spin.setAccessibleName(f"ROI {labels[key]}")
            self.roi_spins[key] = spin
            custom_form.addRow(labels[key], spin)
        layout.addWidget(self.custom_roi_widget)

        self.preview_button = QPushButton("预览区域", panel)
        self.preview_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        )
        self.preview_button.setToolTip("生成开头、中段和结尾的字幕区域拼图")
        self.preview_button.setAccessibleName("预览字幕区域")
        layout.addWidget(self.preview_button)

        layout.addWidget(_section_label("基础参数"))
        basic = _form()
        self.fps = QDoubleSpinBox(panel)
        self.fps.setRange(1.0, 12.0)
        self.fps.setDecimals(1)
        self.fps.setValue(float(DEFAULT_FPS))
        self.fps.setSuffix(" fps")
        self.fps.setAccessibleName("抽帧率")
        basic.addRow("抽帧率", self.fps)

        self.workers = QSpinBox(panel)
        self.workers.setRange(1, max(4, min(32, os.cpu_count() or 4)))
        self.workers.setValue(min(DEFAULT_WORKERS, self.workers.maximum()))
        self.workers.setAccessibleName("OCR 进程数")
        basic.addRow("OCR 进程", self.workers)

        self.style_combo = QComboBox(panel)
        for label, value in (("整行字幕", "line"), ("打字机", "typewriter"), ("自动", "auto")):
            self.style_combo.addItem(label, value)
        self.style_combo.setCurrentIndex(self.style_combo.findData(DEFAULT_STYLE))
        self.style_combo.setAccessibleName("字幕样式")
        basic.addRow("字幕样式", self.style_combo)

        self.offset_ms = QSpinBox(panel)
        self.offset_ms.setRange(-30_000, 30_000)
        self.offset_ms.setSingleStep(50)
        self.offset_ms.setSuffix(" ms")
        self.offset_ms.setValue(DEFAULT_OFFSET_MS)
        self.offset_ms.setAccessibleName("全局时间偏移")
        basic.addRow("时间偏移", self.offset_ms)
        layout.addLayout(basic)

        self.advanced = CollapsibleSection("高级设置", False, panel)
        advanced_form = _form()
        self.confidence = QSpinBox(panel)
        self.confidence.setRange(0, 100)
        self.confidence.setValue(round(DEFAULT_CONFIDENCE_THRESHOLD * 100))
        self.confidence.setSuffix(" %")
        advanced_form.addRow("最低置信度", self.confidence)
        self.same_threshold = QSpinBox(panel)
        self.same_threshold.setRange(0, 100)
        self.same_threshold.setValue(round(DEFAULT_SAME_THRESHOLD))
        self.same_threshold.setSuffix(" %")
        advanced_form.addRow("相似度阈值", self.same_threshold)
        self.confirm_frames = QSpinBox(panel)
        self.confirm_frames.setRange(1, 10)
        self.confirm_frames.setValue(DEFAULT_SWITCH_CONFIRM_FRAMES)
        advanced_form.addRow("切行确认帧", self.confirm_frames)
        self.min_gap_ms = QSpinBox(panel)
        self.min_gap_ms.setRange(0, 10_000)
        self.min_gap_ms.setValue(DEFAULT_MIN_LINE_GAP_MS)
        self.min_gap_ms.setSuffix(" ms")
        advanced_form.addRow("最小行间隔", self.min_gap_ms)
        self.max_blank_frames = QSpinBox(panel)
        self.max_blank_frames.setRange(0, 20)
        self.max_blank_frames.setValue(DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE)
        advanced_form.addRow("行内空白帧", self.max_blank_frames)
        advanced_wrap = QWidget(panel)
        advanced_wrap.setLayout(advanced_form)
        self.advanced.content_layout().addWidget(advanced_wrap)

        self.resume_check = QCheckBox("复用匹配缓存", panel)
        self.resume_check.setChecked(DEFAULT_RESUME)
        self.keep_workdir_check = QCheckBox("保留抽帧文件", panel)
        self.force_check = QCheckBox("允许覆盖输出", panel)
        self.strict_check = QCheckBox("严格时间顺序", panel)
        for checkbox in (
            self.resume_check,
            self.keep_workdir_check,
            self.force_check,
            self.strict_check,
        ):
            self.advanced.content_layout().addWidget(checkbox)
        layout.addWidget(self.advanced)
        layout.addStretch(1)

        scroll.setWidget(panel)
        return scroll

    def _build_result_pane(self) -> QWidget:
        pane = QWidget(self)
        pane.setObjectName("resultPane")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(12, 14, 14, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.addWidget(_section_label("字幕区域预览"))
        header.addStretch(1)
        self.clear_preview_button = _tool_button(
            pane,
            QStyle.StandardPixmap.SP_DialogResetButton,
            "清除预览",
        )
        header.addWidget(self.clear_preview_button)
        layout.addLayout(header)

        self.result_splitter = QSplitter(Qt.Orientation.Vertical, pane)
        self.result_splitter.setChildrenCollapsible(False)
        self.preview_label = AspectPreviewLabel("尚未生成区域预览", self.result_splitter)
        self.result_tabs = QTabWidget(self.result_splitter)
        self.result_splitter.addWidget(self.preview_label)
        self.result_splitter.addWidget(self.result_tabs)
        self.result_splitter.setSizes([380, 300])

        self.result_tabs.addTab(self._build_lrc_tab(), "LRC")
        self.result_tabs.addTab(self._build_log_tab(), "日志")
        layout.addWidget(self.result_splitter, 1)
        return pane

    def _build_lrc_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        self.result_summary = QLabel("0 行", tab)
        self.result_summary.setProperty("secondary", True)
        self.copy_lrc_button = _tool_button(
            tab,
            QStyle.StandardPixmap.SP_FileIcon,
            "复制 LRC",
        )
        self.open_lrc_button = _tool_button(
            tab,
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "打开 LRC",
        )
        self.open_output_folder_button = _tool_button(
            tab,
            QStyle.StandardPixmap.SP_DirOpenIcon,
            "打开输出目录",
        )
        for button in (
            self.copy_lrc_button,
            self.open_lrc_button,
            self.open_output_folder_button,
        ):
            button.setEnabled(False)
        toolbar.addWidget(self.result_summary)
        toolbar.addStretch(1)
        toolbar.addWidget(self.copy_lrc_button)
        toolbar.addWidget(self.open_lrc_button)
        toolbar.addWidget(self.open_output_folder_button)
        layout.addLayout(toolbar)

        self.lrc_preview = QPlainTextEdit(tab)
        self.lrc_preview.setReadOnly(True)
        self.lrc_preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.lrc_preview.setPlaceholderText("LRC 结果将在这里显示")
        self.lrc_preview.setAccessibleName("LRC 结果预览")
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.lrc_preview.setFont(font)
        layout.addWidget(self.lrc_preview, 1)
        return tab

    def _build_log_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        self.clear_log_button = _tool_button(
            tab,
            QStyle.StandardPixmap.SP_DialogResetButton,
            "清空日志",
        )
        toolbar.addWidget(self.clear_log_button)
        layout.addLayout(toolbar)
        self.log_view = QPlainTextEdit(tab)
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setAccessibleName("运行日志")
        layout.addWidget(self.log_view, 1)
        return tab

    def _build_run_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("runBar")
        bar.setFixedHeight(66)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 14, 10)
        layout.setSpacing(10)
        status_layout = QVBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(5)
        self.stage_label = QLabel("就绪", bar)
        self.stage_label.setMinimumWidth(150)
        self.progress = QProgressBar(bar)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("任务进度")
        status_layout.addWidget(self.stage_label)
        status_layout.addWidget(self.progress)
        layout.addLayout(status_layout, 1)

        self.run_button = QPushButton("生成 LRC", bar)
        self.run_button.setProperty("role", "primary")
        self.run_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.run_button.setMinimumWidth(112)
        self.run_button.setFixedHeight(38)
        self.run_button.setAccessibleName("生成 LRC")
        self.cancel_button = QPushButton("取消", bar)
        self.cancel_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.cancel_button.setMinimumWidth(86)
        self.cancel_button.setFixedHeight(38)
        self.cancel_button.setEnabled(False)
        layout.addWidget(self.run_button)
        layout.addWidget(self.cancel_button)
        return bar

    def _connect_signals(self) -> None:
        self.video_field.pathChanged.connect(self._on_video_path_changed)
        self.output_field.pathChanged.connect(self._validate_form)
        self.roi_mode.currentIndexChanged.connect(self._update_roi_mode)
        self.bottom_ratio.valueChanged.connect(self._validate_form)
        self.active_row.currentIndexChanged.connect(self._validate_form)
        for spin in self.roi_spins.values():
            spin.valueChanged.connect(self._validate_form)
        for widget in (
            self.fps,
            self.workers,
            self.style_combo,
            self.offset_ms,
            self.confidence,
            self.same_threshold,
            self.confirm_frames,
            self.min_gap_ms,
            self.max_blank_frames,
        ):
            signal = getattr(widget, "valueChanged", None) or getattr(
                widget, "currentIndexChanged"
            )
            signal.connect(self._validate_form)
        self.force_check.toggled.connect(self._validate_form)

        self.preview_button.clicked.connect(self._start_preview)
        self.run_button.clicked.connect(self._start_run)
        self.cancel_button.clicked.connect(self._runner.cancel)
        self.clear_preview_button.clicked.connect(self.preview_label.clear_preview)
        self.clear_log_button.clicked.connect(self.log_view.clear)
        self.copy_lrc_button.clicked.connect(self._copy_lrc)
        self.open_lrc_button.clicked.connect(self._open_lrc)
        self.open_output_folder_button.clicked.connect(self._open_output_folder)
        self.open_run_dir_button.clicked.connect(self._open_run_dir)
        self.reset_parameters_button.clicked.connect(self._confirm_reset_parameters)

        self._runner.state_changed.connect(self._on_runner_state)
        self._runner.event_received.connect(self._on_event)
        self._runner.progress_changed.connect(self._on_progress)
        self._runner.log_received.connect(self._append_log)
        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.preview_ready.connect(self._on_preview_ready)
        self._runner.failed.connect(self._on_failed)
        self._runner.cancelled.connect(self._on_cancelled)
        self._runner.finished.connect(self._on_finished)

    def _default_workdir(self) -> Path:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Video2LRC" / "work"
        root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.GenericDataLocation
        )
        return Path(root or (Path.home() / "AppData" / "Local")) / "Video2LRC" / "work"

    def _build_job_spec(self) -> JobSpec:
        roi: ROI | None = None
        crop_bottom_ratio: float | None = None
        if self.roi_mode.currentData() == "custom":
            roi = ROI(
                self.roi_spins["x"].value() / 100.0,
                self.roi_spins["y"].value() / 100.0,
                self.roi_spins["width"].value() / 100.0,
                self.roi_spins["height"].value() / 100.0,
            )
        else:
            crop_bottom_ratio = self.bottom_ratio.value() / 100.0
        return JobSpec(
            video_path=self.video_field.path(),
            output_path=self.output_field.path() or None,
            workdir=self._default_workdir(),
            fps=self.fps.value(),
            workers=self.workers.value(),
            roi=roi,
            crop_bottom_ratio=crop_bottom_ratio,
            style=str(self.style_combo.currentData()),
            active_row=str(self.active_row.currentData()),
            offset_ms=self.offset_ms.value(),
            confidence_threshold=self.confidence.value() / 100.0,
            same_threshold=float(self.same_threshold.value()),
            switch_confirm_frames=self.confirm_frames.value(),
            min_line_gap_ms=self.min_gap_ms.value(),
            max_blank_frames_inside_line=self.max_blank_frames.value(),
            keep_workdir=self.keep_workdir_check.isChecked(),
            resume=self.resume_check.isChecked(),
            force=self.force_check.isChecked(),
            strict=self.strict_check.isChecked(),
        )

    def set_video_path(self, path: str | Path) -> None:
        selected = Path(path).expanduser()
        self.video_field.set_path(selected)

    @Slot(str)
    def _on_video_path_changed(self, value: str) -> None:
        if self._restoring:
            return
        path = Path(value).expanduser() if value else None
        previous_default = (
            self._last_video_path.with_suffix(".lrc")
            if self._last_video_path is not None and self._last_video_path.suffix
            else None
        )
        output_value = self.output_field.path()
        output_is_default = (
            not output_value
            or (previous_default is not None and Path(output_value) == previous_default)
        )
        self._last_video_path = path
        if path is not None and path.suffix and output_is_default:
            self.output_field.set_path(path.with_suffix(".lrc"))
        if path is not None:
            self.statusBar().showMessage(path.name)
        else:
            self.statusBar().clearMessage()
        self.preview_label.clear_preview()
        self._validate_form()

    @Slot()
    def _update_roi_mode(self) -> None:
        custom = self.roi_mode.currentData() == "custom"
        self.custom_roi_widget.setVisible(custom)
        self.bottom_ratio.setEnabled(not custom)
        self._validate_form()

    @Slot()
    def _confirm_reset_parameters(self) -> None:
        if self._runner.is_running:
            return
        answer = QMessageBox.question(
            self,
            "恢复默认参数",
            "将所有处理参数恢复为初始推荐值？\n\n视频和输出路径不会改变。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._reset_parameters()

    def _reset_parameters(self) -> None:
        self.roi_mode.setCurrentIndex(self.roi_mode.findData("bottom"))
        self.bottom_ratio.setValue(DEFAULT_BOTTOM_RATIO_PERCENT)
        self.active_row.setCurrentIndex(self.active_row.findData(DEFAULT_ACTIVE_ROW))
        for key, value in DEFAULT_CUSTOM_ROI_PERCENT.items():
            self.roi_spins[key].setValue(value)

        self.fps.setValue(float(DEFAULT_FPS))
        self.workers.setValue(min(DEFAULT_WORKERS, self.workers.maximum()))
        self.style_combo.setCurrentIndex(self.style_combo.findData(DEFAULT_STYLE))
        self.offset_ms.setValue(DEFAULT_OFFSET_MS)

        self.confidence.setValue(round(DEFAULT_CONFIDENCE_THRESHOLD * 100))
        self.same_threshold.setValue(round(DEFAULT_SAME_THRESHOLD))
        self.confirm_frames.setValue(DEFAULT_SWITCH_CONFIRM_FRAMES)
        self.min_gap_ms.setValue(DEFAULT_MIN_LINE_GAP_MS)
        self.max_blank_frames.setValue(DEFAULT_MAX_BLANK_FRAMES_INSIDE_LINE)
        self.resume_check.setChecked(DEFAULT_RESUME)
        self.keep_workdir_check.setChecked(False)
        self.force_check.setChecked(False)
        self.strict_check.setChecked(False)
        self.advanced.set_expanded(False)

        self.preview_label.clear_preview()
        self._update_roi_mode()
        self._validate_form()
        self.statusBar().showMessage(
            "参数已恢复为初始推荐值；视频和输出路径保持不变",
            4000,
        )

    @Slot()
    def _validate_form(self) -> None:
        if not hasattr(self, "run_button"):
            return
        active = self._runner.state in ACTIVE_STATES
        message = ""
        status = "info"
        valid = True
        try:
            spec = self._build_job_spec()
            video = Path(spec.video_path)
            if not video.is_file():
                raise ValueError("请选择存在的视频文件")
            if video.suffix.lower() not in VIDEO_SUFFIXES:
                raise ValueError("不支持该视频格式")
            output = spec.default_output_path
            if output.exists() and not spec.force:
                valid = False
                message = "输出文件已存在；启用“允许覆盖输出”后可继续"
                status = "warning"
        except (OSError, ValueError) as exc:
            valid = False
            message = str(exc)
            status = "error" if self.video_field.path() else "info"

        self.run_button.setEnabled(valid and not active)
        preview_valid = bool(self.video_field.path())
        if preview_valid:
            try:
                spec = self._build_job_spec()
                preview_valid = Path(spec.video_path).is_file()
            except (OSError, ValueError):
                preview_valid = False
        self.preview_button.setEnabled(preview_valid and not active)
        if message and self.video_field.path():
            self.banner.set_status(message, status)
            self.banner.setVisible(True)
        elif self.banner.status() in {"warning", "error"} or not message:
            self.banner.setVisible(False)

    @Slot()
    def _start_preview(self) -> None:
        self._start_job("preview")

    @Slot()
    def _start_run(self) -> None:
        self._start_job("run")

    def _start_job(self, mode: str) -> None:
        if self._runner.is_running:
            return
        try:
            spec = self._build_job_spec()
        except ValueError as exc:
            self._show_banner(str(exc), "error")
            return
        output = spec.default_output_path
        if mode == "run" and output.exists() and not spec.force:
            self._show_banner("输出文件已存在；启用“允许覆盖输出”后可继续", "warning")
            return

        self._current_mode = mode
        self._current_result = None
        self.progress.setValue(0)
        self.result_tabs.setCurrentIndex(1)
        self._append_log(f"开始{'区域预览' if mode == 'preview' else '生成 LRC'}：{spec.video_path}")
        self._show_banner("任务已启动", "info")
        try:
            self._runner.start(spec, preview=mode == "preview")
        except (OSError, RuntimeError, ValueError) as exc:
            self._show_banner(str(exc), "error")

    @Slot(str)
    def _on_runner_state(self, state_value: str) -> None:
        state = RunnerState(state_value)
        active = state in ACTIVE_STATES
        self.setAcceptDrops(not active)
        self.settings_panel.setEnabled(not active)
        self.run_button.setEnabled(False if active else self.run_button.isEnabled())
        self.preview_button.setEnabled(False if active else self.preview_button.isEnabled())
        self.cancel_button.setEnabled(active and state is not RunnerState.CANCELLING)
        if state is RunnerState.STARTING:
            self.stage_label.setText("启动任务")
            self.progress.setRange(0, 0)
        elif state is RunnerState.RUNNING:
            self.progress.setRange(0, 100)
        elif state is RunnerState.CANCELLING:
            self.stage_label.setText("正在取消")
            self.cancel_button.setEnabled(False)
            self._show_banner("正在取消；缓存将保留", "info")

    @Slot(dict)
    def _on_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        stage = str(event.get("stage", ""))
        if event_type == "stage_started":
            self.stage_label.setText(STAGE_LABELS.get(stage, stage or "处理中"))
            start, _ = STAGE_RANGES.get(stage, (0, 100))
            self.progress.setRange(0, 100)
            self.progress.setValue(start)
        elif event_type == "stage_completed":
            _, end = STAGE_RANGES.get(stage, (0, self.progress.value()))
            self.progress.setValue(end)
            if event.get("reused"):
                self._append_log(f"已复用缓存：{STAGE_LABELS.get(stage, stage)}")

    @Slot(dict)
    def _on_progress(self, event: dict[str, Any]) -> None:
        stage = str(event.get("stage", ""))
        start, end = STAGE_RANGES.get(stage, (0, 100))
        try:
            ratio = max(0.0, min(1.0, float(event.get("ratio", 0.0))))
        except (TypeError, ValueError):
            ratio = 0.0
        value = round(start + (end - start) * ratio)
        self.progress.setRange(0, 100)
        self.progress.setValue(value)
        current = event.get("current")
        total = event.get("total")
        if current is not None and total is not None:
            self.stage_label.setText(
                f"{STAGE_LABELS.get(stage, stage or '处理中')}  {current}/{total}"
            )

    @Slot(str)
    def _append_log(self, message: str) -> None:
        if not message:
            return
        self.log_view.appendPlainText(message)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )

    @Slot(dict)
    def _on_succeeded(self, result: dict[str, Any]) -> None:
        self._current_result = dict(result)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        run_dir = result.get("run_dir")
        self._run_dir = Path(run_dir) if isinstance(run_dir, str) else None
        self.open_run_dir_button.setEnabled(
            self._run_dir is not None and self._run_dir.is_dir()
        )
        if self._current_mode == "preview":
            self.stage_label.setText("区域预览完成")
            self._show_banner("区域预览已更新", "success")
            return

        output = result.get("output_path")
        review = result.get("review_path")
        self._output_path = Path(output) if isinstance(output, str) else None
        self._review_path = Path(review) if isinstance(review, str) else None
        if self._output_path is None or not self._output_path.is_file():
            self._on_failed("任务完成，但 LRC 文件不可读取")
            return
        try:
            content = self._output_path.read_text(encoding="utf-8")
        except OSError as exc:
            self._on_failed(f"无法读取 LRC：{exc}")
            return
        self.lrc_preview.setPlainText(content)
        line_count = len(content.splitlines())
        self.result_summary.setText(f"{line_count} 行")
        self.copy_lrc_button.setEnabled(bool(content))
        self.open_lrc_button.setEnabled(True)
        self.open_output_folder_button.setEnabled(True)
        self.result_tabs.setCurrentIndex(0)
        self.stage_label.setText("生成完成")
        self._show_banner(f"已生成 {line_count} 行 LRC", "success")
        self.statusBar().showMessage(str(self._output_path))

    @Slot(str)
    def _on_preview_ready(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._show_banner("预览任务完成，但图片无法读取", "error")
            return
        self.preview_label.setPixmap(pixmap)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.stage_label.setText("处理失败")
        self._show_banner(message, "error")
        self._append_log(f"错误：{message}")
        self.result_tabs.setCurrentIndex(1)

    @Slot(str)
    def _on_cancelled(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.stage_label.setText("已取消")
        self._show_banner(message or "任务已取消；缓存已保留", "info")
        self._append_log(message or "任务已取消")

    @Slot(int)
    def _on_finished(self, _exit_code: int) -> None:
        terminal_text = self.banner.text()
        terminal_status = self.banner.status()
        self.cancel_button.setEnabled(False)
        self.settings_panel.setEnabled(True)
        self._validate_form()
        if self._runner.state in {
            RunnerState.SUCCEEDED,
            RunnerState.FAILED,
            RunnerState.CANCELLED,
        }:
            self._show_banner(terminal_text, terminal_status)
        if self._close_after_finish:
            self._close_after_finish = False
            QTimer.singleShot(0, self.close)

    def _show_banner(self, text: str, status: str) -> None:
        self.banner.set_status(text, status)
        self.banner.setVisible(True)

    @Slot()
    def _copy_lrc(self) -> None:
        QGuiApplication.clipboard().setText(self.lrc_preview.toPlainText())
        self.statusBar().showMessage("LRC 已复制", 2500)

    @Slot()
    def _open_lrc(self) -> None:
        if self._output_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_path)))

    @Slot()
    def _open_output_folder(self) -> None:
        if self._output_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_path.parent)))

    @Slot()
    def _open_run_dir(self) -> None:
        if self._run_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._run_dir)))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._runner.state in ACTIVE_STATES:
            event.ignore()
            return
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() in VIDEO_SUFFIXES:
                event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if self._runner.state in ACTIVE_STATES:
            event.ignore()
            return
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.set_video_path(urls[0].toLocalFile())
            event.acceptProposedAction()

    def _remove_invalid_setting(self, key: str) -> None:
        self._settings.remove(key)

    def _restore_qt_state(self, key: str, restore: Any) -> None:
        value = self._settings.value(key)
        if value is None:
            return
        try:
            restored = bool(restore(value))
        except (OverflowError, RuntimeError, TypeError, ValueError):
            restored = False
        if not restored:
            self._remove_invalid_setting(key)

    def _bounded_setting(
        self,
        key: str,
        default: int | float,
        converter: Any,
        minimum: int | float,
        maximum: int | float,
    ) -> int | float:
        value = self._settings.value(key, default)
        try:
            converted = converter(value)
            if isinstance(converted, float) and not math.isfinite(converted):
                raise ValueError("setting must be finite")
            if converted < minimum or converted > maximum:
                raise ValueError("setting is outside the supported range")
        except (OverflowError, TypeError, ValueError):
            self._remove_invalid_setting(key)
            return default
        return converted

    def _bool_setting(self, key: str, default: bool) -> bool:
        value = self._settings.value(key, default)
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        self._remove_invalid_setting(key)
        return default

    def _restore_settings(self) -> None:
        self._restoring = True
        try:
            self._restore_qt_state("window/geometry", self.restoreGeometry)
            self._restore_qt_state(
                "window/main_splitter",
                self.main_splitter.restoreState,
            )
            self._restore_qt_state(
                "window/result_splitter",
                self.result_splitter.restoreState,
            )
            self.bottom_ratio.setValue(
                int(
                    self._bounded_setting(
                        "options/bottom_ratio",
                        DEFAULT_BOTTOM_RATIO_PERCENT,
                        int,
                        self.bottom_ratio.minimum(),
                        self.bottom_ratio.maximum(),
                    )
                )
            )
            self.fps.setValue(
                float(
                    self._bounded_setting(
                        "options/fps",
                        float(DEFAULT_FPS),
                        float,
                        self.fps.minimum(),
                        self.fps.maximum(),
                    )
                )
            )
            self.workers.setValue(
                int(
                    self._bounded_setting(
                        "options/workers",
                        min(DEFAULT_WORKERS, self.workers.maximum()),
                        int,
                        self.workers.minimum(),
                        self.workers.maximum(),
                    )
                )
            )
            self.offset_ms.setValue(
                int(
                    self._bounded_setting(
                        "options/offset_ms",
                        DEFAULT_OFFSET_MS,
                        int,
                        self.offset_ms.minimum(),
                        self.offset_ms.maximum(),
                    )
                )
            )
            self.resume_check.setChecked(
                self._bool_setting("options/resume", DEFAULT_RESUME)
            )
            self.advanced.set_expanded(
                self._bool_setting("options/advanced", False)
            )
        finally:
            self._restoring = False

    def _save_settings(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("window/main_splitter", self.main_splitter.saveState())
        self._settings.setValue(
            "window/result_splitter", self.result_splitter.saveState()
        )
        self._settings.setValue("options/bottom_ratio", self.bottom_ratio.value())
        self._settings.setValue("options/fps", self.fps.value())
        self._settings.setValue("options/workers", self.workers.value())
        self._settings.setValue("options/offset_ms", self.offset_ms.value())
        self._settings.setValue("options/resume", self.resume_check.isChecked())
        self._settings.setValue("options/advanced", self.advanced.is_expanded())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._runner.is_running:
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "取消当前任务并关闭窗口？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if not self._runner.is_running:
                self._save_settings()
                super().closeEvent(event)
                return
            self._close_after_finish = True
            self._runner.cancel()
            event.ignore()
            return
        self._save_settings()
        super().closeEvent(event)


__all__ = ["MainWindow", "STAGE_LABELS", "STAGE_RANGES", "VIDEO_SUFFIXES"]
