from dataclasses import dataclass, field
from datetime import datetime


@dataclass(kw_only=True)
class Notification:
    event_id: str
    operation: str
    status: str
    timestamp: datetime
    pages_processed: int | None = None


@dataclass(kw_only=True)
class OperationTiming:
    started_at: datetime | None = None
    ended_at: datetime | None = None
    pages_processed: int | None = None

    @property
    def duration_seconds(self) -> float:
        seconds = (self.ended_at - self.started_at).total_seconds()
        if seconds < 0:
            raise ValueError("Operation ended before it started")
        return seconds


@dataclass(kw_only=True)
class EventTiming:
    event_id: str
    received_at: datetime | None = None
    flow: OperationTiming = field(default_factory=OperationTiming)
    operators: dict[str, OperationTiming] = field(default_factory=dict)

    @property
    def pages_processed(self) -> int | None:
        if self.flow.pages_processed is not None:
            return self.flow.pages_processed
        # Each streaming event represents one file; its extracted pages are the shared stage workload.
        counts = [timing.pages_processed for timing in self.operators.values() if timing.pages_processed is not None]
        return max(counts) if counts else None
