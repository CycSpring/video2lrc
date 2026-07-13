"""Incremental parser for the pipeline's stdout event stream."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


EVENT_PREFIX = "V2LRC_EVENT "
MAX_EVENT_LINE_BYTES = 1024 * 1024
_EVENT_PREFIX_BYTES = EVENT_PREFIX.encode("ascii")


class ProtocolError(ValueError):
    """Raised when a prefixed stdout line is not a valid event."""


@dataclass(frozen=True, slots=True)
class ProtocolIssue:
    message: str


def parse_event_line(
    line: bytes | bytearray | memoryview | str,
    *,
    max_line_bytes: int = MAX_EVENT_LINE_BYTES,
) -> dict[str, Any] | None:
    """Parse one stdout line, returning ``None`` for normal process output."""

    if max_line_bytes < len(_EVENT_PREFIX_BYTES) + 2:
        raise ValueError("max_line_bytes is too small")
    if isinstance(line, str):
        raw = line.rstrip("\r\n").encode("utf-8")
    else:
        raw = bytes(line).rstrip(b"\r\n")
    if not raw.startswith(_EVENT_PREFIX_BYTES):
        return None
    if len(raw) > max_line_bytes:
        raise ProtocolError(f"event line exceeds the {max_line_bytes}-byte limit")
    payload = raw[len(_EVENT_PREFIX_BYTES) :]
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("event payload is not valid UTF-8") from exc
    try:
        event = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"event payload is not valid JSON: {exc.msg}") from exc
    if not isinstance(event, dict):
        raise ProtocolError("event payload must be a JSON object")

    event_type = event.get("type")
    compatibility_type = event.get("event")
    if event_type is None:
        event_type = compatibility_type
        if event_type is not None:
            event["type"] = event_type
    if not isinstance(event_type, str) or not event_type:
        raise ProtocolError("event payload must contain a non-empty string 'type'")
    if compatibility_type is not None and compatibility_type != event_type:
        raise ProtocolError("event payload has conflicting 'type' and 'event' values")
    return event


class EventStreamParser:
    """Parse arbitrarily chunked JSONL without retaining unbounded stdout."""

    def __init__(self, *, max_line_bytes: int = MAX_EVENT_LINE_BYTES) -> None:
        if max_line_bytes < len(_EVENT_PREFIX_BYTES) + 2:
            raise ValueError("max_line_bytes is too small")
        self.max_line_bytes = max_line_bytes
        self._buffer = bytearray()
        self._discarding_oversize = False
        self._issues: list[ProtocolIssue] = []

    @property
    def issues(self) -> tuple[ProtocolIssue, ...]:
        return tuple(self._issues)

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def pop_issues(self) -> list[ProtocolIssue]:
        issues = self._issues
        self._issues = []
        return issues

    def reset(self) -> None:
        self._buffer.clear()
        self._discarding_oversize = False
        self._issues.clear()

    def feed(self, data: bytes | bytearray | memoryview | str) -> list[dict[str, Any]]:
        if isinstance(data, str):
            chunk = data.encode("utf-8")
        else:
            chunk = bytes(data)
        if not chunk:
            return []

        if self._discarding_oversize:
            newline = chunk.find(b"\n")
            if newline < 0:
                return []
            self._discarding_oversize = False
            chunk = chunk[newline + 1 :]

        self._buffer.extend(chunk)
        events: list[dict[str, Any]] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            self._consume(line, events)

        if len(self._buffer) > self.max_line_bytes:
            if self._buffer.startswith(_EVENT_PREFIX_BYTES):
                self._issues.append(
                    ProtocolIssue(
                        f"event line exceeds the {self.max_line_bytes}-byte limit"
                    )
                )
            self._buffer.clear()
            self._discarding_oversize = True
        return events

    def finish(self) -> list[dict[str, Any]]:
        """Parse a final unterminated line after the process exits."""

        events: list[dict[str, Any]] = []
        if self._discarding_oversize:
            self._discarding_oversize = False
        elif self._buffer:
            line = bytes(self._buffer)
            self._buffer.clear()
            self._consume(line, events)
        return events

    def _consume(self, line: bytes, events: list[dict[str, Any]]) -> None:
        if not line.startswith(_EVENT_PREFIX_BYTES):
            return
        try:
            event = parse_event_line(line, max_line_bytes=self.max_line_bytes)
        except ProtocolError as exc:
            self._issues.append(ProtocolIssue(str(exc)))
            return
        if event is not None:
            events.append(event)
