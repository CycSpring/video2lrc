from __future__ import annotations

import json

import pytest

from video2lrc_ui.protocol import (
    EVENT_PREFIX,
    EventStreamParser,
    ProtocolError,
    parse_event_line,
)


def event_line(payload: dict[str, object]) -> bytes:
    return (EVENT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def test_parser_handles_split_utf8_and_multiple_lines() -> None:
    parser = EventStreamParser()
    first = event_line({"type": "progress", "current": 1, "status": "识别中"})
    second = event_line({"type": "completed", "result": {"line_count": 2}})
    split_at = first.index("识".encode("utf-8")) + 1

    assert parser.feed(b"ordinary log\n" + first[:split_at]) == []
    events = parser.feed(first[split_at:] + second)

    assert [event["type"] for event in events] == ["progress", "completed"]
    assert events[0]["status"] == "识别中"
    assert parser.issues == ()


def test_parser_ignores_arbitrary_ordinary_stdout() -> None:
    parser = EventStreamParser(max_line_bytes=64)

    assert parser.feed(b"plain output\nnot json\n") == []
    assert parser.feed(b"x" * 100) == []
    assert parser.feed(b"continued then discarded\n") == []
    assert parser.issues == ()
    assert parser.buffered_bytes == 0


def test_parser_reports_malformed_prefixed_lines_and_recovers() -> None:
    parser = EventStreamParser()
    data = (
        (EVENT_PREFIX + "{broken}\n").encode("ascii")
        + event_line({"type": "progress", "current": 2, "total": 3})
    )

    events = parser.feed(data)

    assert events == [{"type": "progress", "current": 2, "total": 3}]
    assert len(parser.issues) == 1
    assert "valid JSON" in parser.issues[0].message


def test_parser_caps_unterminated_event_lines_and_recovers() -> None:
    parser = EventStreamParser(max_line_bytes=64)
    oversized = EVENT_PREFIX.encode("ascii") + b'{' + b"x" * 100

    assert parser.feed(oversized) == []
    assert parser.buffered_bytes == 0
    assert len(parser.issues) == 1

    valid = event_line({"type": "cancelled", "message": "stopped"})
    assert parser.feed(b"tail of oversized line\n" + valid) == [
        {"type": "cancelled", "message": "stopped"}
    ]


def test_parser_finish_accepts_an_unterminated_final_event() -> None:
    parser = EventStreamParser()
    line = event_line({"event": "completed", "result": {}}).rstrip(b"\n")

    assert parser.feed(line) == []
    assert parser.finish() == [{"event": "completed", "result": {}, "type": "completed"}]


@pytest.mark.parametrize(
    "line",
    [
        EVENT_PREFIX + "[]",
        EVENT_PREFIX + '{}',
        EVENT_PREFIX + '{"type":"one","event":"two"}',
    ],
)
def test_parse_event_line_rejects_invalid_event_shapes(line: str) -> None:
    with pytest.raises(ProtocolError):
        parse_event_line(line)
