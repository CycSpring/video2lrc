"""Qt desktop UI support for video2lrc."""

from .models import ROI, JobSpec
from .process_runner import ProcessRunner, RunnerState
from .protocol import EVENT_PREFIX, EventStreamParser

__all__ = [
    "EVENT_PREFIX",
    "EventStreamParser",
    "JobSpec",
    "ProcessRunner",
    "ROI",
    "RunnerState",
]
