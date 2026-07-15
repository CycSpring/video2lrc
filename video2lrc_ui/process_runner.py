"""Asynchronous, cancellable QProcess bridge to the video2lrc CLI."""

from __future__ import annotations

import codecs
import os
import re
import shutil
import sys
import tempfile
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import (
    QObject,
    QProcess,
    QProcessEnvironment,
    QTimer,
    Signal,
    Slot,
)

from .models import JobSpec
from .protocol import EventStreamParser


CANCEL_TIMEOUT_MS = 15_000
WINDOWS_CREATE_NO_WINDOW = 0x08000000
ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\))|"
    r"(?:(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~])|"
    r"(?:\x1b[@-_])"
)
RAPIDOCR_EMPTY_WARNING_MESSAGES = (
    "the text detection result is empty",
    "the identified content is empty",
)


def strip_ansi_sequences(text: str) -> str:
    """Remove terminal formatting sequences before showing process logs in Qt."""

    return ANSI_ESCAPE_RE.sub("", text)


def is_routine_rapidocr_empty_warning(text: str) -> bool:
    """Return whether a line is RapidOCR's expected no-text frame warning."""

    normalized = strip_ansi_sequences(text).casefold()
    return (
        "[warning]" in normalized
        and "[rapidocr]" in normalized
        and any(message in normalized for message in RAPIDOCR_EMPTY_WARNING_MESSAGES)
    )


def configure_windows_no_window(
    process: QProcess,
    *,
    platform_name: str | None = None,
) -> Any:
    """Apply ``CREATE_NO_WINDOW`` when the Python binding exposes the hook.

    Qt's Windows backend already adds this flag when the parent has no console,
    while retaining its named stdout/stderr pipes. PySide currently omits the
    public modifier binding, so keeping the default is the correct fallback.
    """

    if (platform_name or os.name) != "nt":
        return None
    setter = getattr(process, "setCreateProcessArgumentsModifier", None)
    if not callable(setter):
        return None

    def add_no_window(arguments: Any) -> None:
        arguments.flags |= WINDOWS_CREATE_NO_WINDOW

    setter(add_no_window)
    return add_no_window


class RunnerState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = frozenset(
    (RunnerState.STARTING, RunnerState.RUNNING, RunnerState.CANCELLING)
)


def build_process_invocation(
    spec: JobSpec,
    *,
    preview: bool = False,
    job_id: str,
    cancel_file: str | os.PathLike[str],
    project_root: str | os.PathLike[str] | None = None,
    frozen: bool | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> tuple[str, list[str], str]:
    """Return ``program, arguments, working_directory`` without using a shell."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    host_executable = Path(executable or sys.executable).resolve()
    if is_frozen:
        working_directory = host_executable.parent
        program = working_directory / "video2lrc-cli.exe"
        prefix: list[str] = []
    else:
        working_directory = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        program = host_executable
        prefix = ["-u", str(working_directory / "main.py")]

    arguments = prefix + spec.to_cli_args(
        preview=preview,
        event_stream=True,
        job_id=job_id,
        cancel_file=cancel_file,
    )
    return str(program), arguments, str(working_directory)


class ProcessRunner(QObject):
    """Run one CLI job at a time and translate its JSONL events to Qt signals."""

    state_changed = Signal(str)
    event_received = Signal(dict)
    progress_changed = Signal(dict)
    log_received = Signal(str)
    succeeded = Signal(dict)
    preview_ready = Signal(str)
    failed = Signal(str)
    cancelled = Signal(str)
    finished = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        project_root: str | os.PathLike[str] | None = None,
        frozen: bool | None = None,
        executable: str | os.PathLike[str] | None = None,
        cancel_timeout_ms: int = CANCEL_TIMEOUT_MS,
    ) -> None:
        super().__init__(parent)
        if cancel_timeout_ms < 0:
            raise ValueError("cancel_timeout_ms cannot be negative")

        self._project_root = Path(project_root).resolve() if project_root else None
        self._frozen = frozen
        self._executable = Path(executable).resolve() if executable else None
        self._cancel_timeout_ms = cancel_timeout_ms
        self._process = QProcess(self)
        self._windows_process_modifier = configure_windows_no_window(self._process)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.started.connect(self._on_started)
        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.finished.connect(self._on_finished)

        self._cancel_timer = QTimer(self)
        self._cancel_timer.setSingleShot(True)
        self._cancel_timer.timeout.connect(self._force_kill_process_tree)

        self._state = RunnerState.IDLE
        self._parser = EventStreamParser()
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._stderr_buffer = ""
        self._stderr_tail = ""
        self._suppressed_empty_ocr_warnings = 0
        self._job_id: str | None = None
        self._cancel_directory: Path | None = None
        self._cancel_file: Path | None = None
        self._terminal_event: dict[str, Any] | None = None
        self._result: dict[str, Any] | None = None
        self._preview_path: str | None = None
        self._terminal_error: str | None = None
        self._outcome_emitted = False
        self._program = ""
        self._arguments: list[str] = []

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state in ACTIVE_STATES

    @property
    def job_id(self) -> str | None:
        return self._job_id

    @property
    def cancel_file(self) -> Path | None:
        return self._cancel_file

    @property
    def result(self) -> dict[str, Any] | None:
        return None if self._result is None else dict(self._result)

    @property
    def preview_path(self) -> str | None:
        return self._preview_path

    @property
    def program(self) -> str:
        return self._program

    @property
    def arguments(self) -> list[str]:
        return self._arguments.copy()

    def start(self, spec: JobSpec, preview: bool = False) -> str:
        """Start a job and return its event-stream job id immediately."""

        if not isinstance(spec, JobSpec):
            raise TypeError("spec must be a JobSpec")
        if self.is_running or self._process.state() != QProcess.ProcessState.NotRunning:
            raise RuntimeError("a video2lrc job is already running")

        self._cleanup_cancel_directory()
        self._parser.reset()
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._stderr_buffer = ""
        self._stderr_tail = ""
        self._suppressed_empty_ocr_warnings = 0
        self._terminal_event = None
        self._result = None
        self._preview_path = None
        self._terminal_error = None
        self._outcome_emitted = False

        self._job_id = uuid.uuid4().hex
        self._cancel_directory = Path(tempfile.mkdtemp(prefix="video2lrc-job-"))
        self._cancel_file = self._cancel_directory / "cancel.request"
        self._program, self._arguments, working_directory = build_process_invocation(
            spec,
            preview=preview,
            job_id=self._job_id,
            cancel_file=self._cancel_file,
            project_root=self._project_root,
            frozen=self._frozen,
            executable=self._executable,
        )

        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        is_frozen = (
            bool(getattr(sys, "frozen", False))
            if self._frozen is None
            else self._frozen
        )
        if is_frozen:
            application_dir = Path(working_directory)
            bundled_bins = (
                application_dir / "bin",
                application_dir / "_internal" / "bin",
            )
            current_path = environment.value("PATH")
            path_parts = [str(path) for path in bundled_bins if path.is_dir()]
            if current_path:
                path_parts.append(current_path)
            if path_parts:
                environment.insert("PATH", os.pathsep.join(path_parts))
        self._process.setProcessEnvironment(environment)
        self._process.setWorkingDirectory(working_directory)
        self._process.setProgram(self._program)
        self._process.setArguments(self._arguments)
        self._set_state(RunnerState.STARTING)
        self._process.start()
        return self._job_id

    @Slot(result=bool)
    def cancel(self) -> bool:
        """Request cooperative cancellation, then enforce it after 15 seconds."""

        if not self.is_running or self._state is RunnerState.CANCELLING:
            return False
        self._set_state(RunnerState.CANCELLING)
        if self._cancel_file is not None:
            try:
                self._cancel_file.parent.mkdir(parents=True, exist_ok=True)
                self._cancel_file.touch(exist_ok=True)
            except OSError as exc:
                self.log_received.emit(f"无法写入取消标记：{exc}")
        self._cancel_timer.start(self._cancel_timeout_ms)
        return True

    @Slot()
    def _on_started(self) -> None:
        if self._state is RunnerState.STARTING:
            self._set_state(RunnerState.RUNNING)

    @Slot()
    def _read_stdout(self) -> None:
        data = bytes(self._process.readAllStandardOutput())
        if not data:
            return
        for event in self._parser.feed(data):
            self._handle_event(event)
        self._report_protocol_issues()

    @Slot()
    def _read_stderr(self) -> None:
        data = bytes(self._process.readAllStandardError())
        if not data:
            return
        self._append_stderr(self._stderr_decoder.decode(data, final=False))

    def _append_stderr(self, text: str) -> None:
        if not text:
            return
        self._stderr_buffer += text
        while "\n" in self._stderr_buffer:
            line, self._stderr_buffer = self._stderr_buffer.split("\n", 1)
            self._emit_stderr_line(line)
        if len(self._stderr_buffer) > 65_536:
            self._emit_stderr_line(self._stderr_buffer)
            self._stderr_buffer = ""

    def _emit_stderr_line(self, line: str) -> None:
        cleaned = strip_ansi_sequences(line.rstrip("\r"))
        if not cleaned.strip():
            return
        if is_routine_rapidocr_empty_warning(cleaned):
            self._suppressed_empty_ocr_warnings += 1
            return
        separator = "\n" if self._stderr_tail else ""
        self._stderr_tail = (self._stderr_tail + separator + cleaned)[-32_768:]
        self.log_received.emit(cleaned)

    def _emit_suppressed_warning_summary(self) -> None:
        count = self._suppressed_empty_ocr_warnings
        if count <= 0:
            return
        self._suppressed_empty_ocr_warnings = 0
        self.log_received.emit(
            f"OCR 提示：已折叠 {count} 条“未检测到字幕文字”的重复日志；"
            "转场或无字幕画面通常属于正常情况。"
        )

    def _report_protocol_issues(self) -> None:
        for issue in self._parser.pop_issues():
            self.log_received.emit(f"事件流格式错误：{issue.message}")

    def _handle_event(self, event: dict[str, Any]) -> None:
        self.event_received.emit(dict(event))
        event_type = event.get("type")
        if event_type == "progress":
            self.progress_changed.emit(dict(event))
            return
        if event_type == "artifact":
            kind = event.get("kind")
            path = event.get("path")
            if kind in {"preview", "roi_preview"} and isinstance(path, str):
                self._preview_path = path
            return
        if event_type not in {"completed", "cancelled", "failed"}:
            return

        self._terminal_event = dict(event)
        if event_type == "completed":
            result = event.get("result")
            if isinstance(result, Mapping):
                self._result = dict(result)
                preview_path = self._result.get("preview_path")
                if isinstance(preview_path, str):
                    self._preview_path = preview_path
            else:
                self._terminal_error = "完成事件缺少有效的 result 对象"
        elif event_type == "cancelled":
            message = event.get("message")
            if isinstance(message, str) and message:
                self._terminal_error = message
        else:
            error = event.get("error")
            if isinstance(error, Mapping):
                message = error.get("message")
                if isinstance(message, str) and message:
                    self._terminal_error = message
            elif isinstance(error, str) and error:
                self._terminal_error = error

    @Slot(QProcess.ProcessError)
    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        message = self._process.errorString() or f"无法启动：{self._program}"
        self._finish_with_failure(message, -1)

    @Slot(int, QProcess.ExitStatus)
    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        if self._outcome_emitted:
            return
        self._read_stdout()
        self._read_stderr()
        for event in self._parser.finish():
            self._handle_event(event)
        self._report_protocol_issues()
        self._append_stderr(self._stderr_decoder.decode(b"", final=True))
        if self._stderr_buffer:
            self._emit_stderr_line(self._stderr_buffer)
            self._stderr_buffer = ""
        self._emit_suppressed_warning_summary()
        self._cancel_timer.stop()

        terminal_type = (
            self._terminal_event.get("type") if self._terminal_event is not None else None
        )
        if (
            exit_status == QProcess.ExitStatus.NormalExit
            and exit_code == 0
            and terminal_type == "completed"
            and self._result is not None
            and self._terminal_error is None
        ):
            self._outcome_emitted = True
            self._set_state(RunnerState.SUCCEEDED)
            result = dict(self._result)
            self.succeeded.emit(result)
            if self._preview_path:
                self.preview_ready.emit(self._preview_path)
            self._cleanup_cancel_directory()
            self.finished.emit(exit_code)
            return

        if terminal_type == "cancelled" or exit_code == 130 or (
            self._state is RunnerState.CANCELLING and terminal_type != "completed"
        ):
            self._outcome_emitted = True
            self._set_state(RunnerState.CANCELLED)
            message = self._terminal_error or "任务已取消"
            self.cancelled.emit(message)
            self._cleanup_cancel_directory()
            self.finished.emit(exit_code)
            return

        if terminal_type == "failed":
            message = self._terminal_error or "处理失败"
        elif exit_status == QProcess.ExitStatus.CrashExit:
            message = self._terminal_error or "处理进程意外退出"
        elif self._terminal_error:
            message = self._terminal_error
        elif self._stderr_tail.strip():
            message = self._stderr_tail.strip().splitlines()[-1]
        elif exit_code == 0:
            message = "处理进程退出，但没有返回完成事件"
        else:
            message = f"处理进程退出，代码 {exit_code}"
        self._finish_with_failure(message, exit_code)

    def _finish_with_failure(self, message: str, exit_code: int) -> None:
        if self._outcome_emitted:
            return
        self._outcome_emitted = True
        self._cancel_timer.stop()
        self._set_state(RunnerState.FAILED)
        self.failed.emit(message)
        self._cleanup_cancel_directory()
        self.finished.emit(exit_code)

    @Slot()
    def _force_kill_process_tree(self) -> None:
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return
        pid = int(self._process.processId())
        if os.name == "nt" and pid > 0:
            started = QProcess.startDetached(
                "taskkill.exe",
                ["/PID", str(pid), "/T", "/F"],
            )
            # PySide versions return either bool or (bool, pid).
            started_ok = started[0] if isinstance(started, tuple) else bool(started)
            if started_ok:
                QTimer.singleShot(1_000, self._kill_if_still_running)
                return
        self._process.kill()

    @Slot()
    def _kill_if_still_running(self) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _set_state(self, state: RunnerState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state.value)

    def _cleanup_cancel_directory(self) -> None:
        directory = self._cancel_directory
        self._cancel_directory = None
        self._cancel_file = None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


# Backward-friendly descriptive name for callers outside the bundled UI.
PipelineProcessRunner = ProcessRunner
