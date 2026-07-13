from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, TextIO, runtime_checkable


EVENT_PREFIX = "V2LRC_EVENT "
TERMINAL_EVENTS = frozenset({"completed", "cancelled", "failed"})
LOGGER = logging.getLogger(__name__)


class PipelineCancelled(RuntimeError):
    """Raised when a cooperative pipeline cancellation is requested."""


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event_type: str, **fields: Any) -> Mapping[str, Any]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported event value: {type(value)!r}")


def build_event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Build the stable event envelope used by CLI and in-process callers."""

    if not event_type or not isinstance(event_type, str):
        raise ValueError("event_type must be a non-empty string")
    job_id = fields.pop("job_id", None)
    sequence = fields.pop("seq", None)
    payload = dict(fields)
    payload.update(
        {
            "type": event_type,
            "event": event_type,
            "job_id": job_id,
            "seq": sequence,
            "timestamp": _utc_now(),
        }
    )
    return payload


class JsonLineEventEmitter:
    """Write prefixed, single-line JSON events for subprocess consumers."""

    def __init__(self, stream: TextIO | None = None, *, job_id: str | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.job_id = job_id or uuid.uuid4().hex
        self.terminal_event: str | None = None
        self._sequence = 0
        self._lock = threading.Lock()

    def emit(self, event_type: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            fields["job_id"] = self.job_id
            fields["seq"] = self._sequence
            payload = build_event(event_type, **fields)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                default=_json_default,
            )
            self.stream.write(f"{EVENT_PREFIX}{encoded}\n")
            self.stream.flush()
            if event_type in TERMINAL_EVENTS:
                self.terminal_event = event_type
        return payload


# Shorter public name for callers that do not care about the transport detail.
EventEmitter = JsonLineEventEmitter


class BestEffortEventSink:
    """Disable a failing event transport without changing pipeline control flow."""

    def __init__(self, sink: Any = None) -> None:
        self.sink = sink
        self.terminal_event: str | None = terminal_event_type(sink)
        self.delivery_error: Exception | None = None

    def emit(self, event_type: str, **fields: Any) -> dict[str, Any]:
        payload = build_event(event_type, **fields)
        if self.delivery_error is not None:
            return payload
        try:
            delivered = emit_event(self.sink, event_type, **fields)
        except Exception as exc:
            self.delivery_error = exc
            LOGGER.warning(
                "Event delivery disabled after %s: %s",
                type(exc).__name__,
                exc,
            )
            return payload
        if event_type in TERMINAL_EVENTS:
            self.terminal_event = event_type
        return delivered


def emit_event(events: Any, event_type: str, **fields: Any) -> dict[str, Any]:
    """Emit to an emitter, list-like collector, or one-argument callback."""

    if events is None:
        return build_event(event_type, **fields)
    emitter = getattr(events, "emit", None)
    if callable(emitter):
        result = emitter(event_type, **fields)
        return dict(result) if isinstance(result, Mapping) else build_event(event_type, **fields)

    payload = build_event(event_type, **fields)
    append = getattr(events, "append", None)
    if callable(append):
        payload["seq"] = len(events) + 1 if hasattr(events, "__len__") else None
        append(payload)
        return payload
    if callable(events):
        events(payload)
        return payload
    raise TypeError("events must provide emit(), append(), or be callable")


def terminal_event_type(events: Any) -> str | None:
    """Return the terminal event already emitted by a supported sink, if known."""

    value = getattr(events, "terminal_event", None)
    if isinstance(value, str) and value in TERMINAL_EVENTS:
        return value
    if isinstance(events, list) and events:
        last = events[-1]
        if isinstance(last, Mapping):
            event_type = last.get("type") or last.get("event")
            if isinstance(event_type, str) and event_type in TERMINAL_EVENTS:
                return event_type
    return None


class CancellationToken:
    """A cooperative cancellation token backed by an optional marker file."""

    def __init__(self, cancel_file: str | Path | None = None) -> None:
        self.cancel_file = (
            Path(cancel_file).expanduser().resolve() if cancel_file is not None else None
        )

    def is_cancelled(self) -> bool:
        return self.cancel_file is not None and self.cancel_file.exists()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise PipelineCancelled("Cancellation requested")


def check_cancellation(cancellation: Any) -> None:
    """Raise PipelineCancelled for supported token/callback representations."""

    if cancellation is None:
        return
    if isinstance(cancellation, (str, Path)):
        CancellationToken(cancellation).raise_if_cancelled()
        return

    checker = getattr(cancellation, "raise_if_cancelled", None)
    if callable(checker):
        checker()
        return

    predicate = getattr(cancellation, "is_cancelled", None)
    if callable(predicate):
        cancelled = predicate()
    elif predicate is not None:
        cancelled = bool(predicate)
    elif callable(cancellation):
        cancelled = bool(cancellation())
    else:
        raise TypeError(
            "cancellation must provide raise_if_cancelled()/is_cancelled(), be a path, "
            "or be callable"
        )
    if cancelled:
        raise PipelineCancelled("Cancellation requested")


__all__ = [
    "BestEffortEventSink",
    "CancellationToken",
    "EVENT_PREFIX",
    "EventEmitter",
    "EventSink",
    "JsonLineEventEmitter",
    "PipelineCancelled",
    "TERMINAL_EVENTS",
    "build_event",
    "check_cancellation",
    "emit_event",
    "terminal_event_type",
]
