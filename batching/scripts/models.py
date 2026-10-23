"""Models for benchmark outcomes, job statistics, and normalized resources."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Environment = Literal["cpu", "gpu", "mps"]
FileType = Literal["pdf-ocr", "pdf-non-ocr", "text"]


@dataclass(kw_only=True)
class JobRunStats:
    environment: Environment
    file_type: FileType
    status: str = "Unknown"
    total_execution_time_seconds: float | None = None
    total_docs: int | None = None
    processed_docs: int | None = None
    completed_docs: int | None = None
    failed_docs: int | None = None
    skipped_docs: int | None = None
    total_pages_processed: int | None = None
    total_chunks: int | None = None
    documents_per_second: float | None = None
    pages_per_second: float | None = None
    operator_timings_seconds: dict[str, float] = field(default_factory=dict)


@dataclass(kw_only=True)
class ResourceSample:
    """Percentages always use physical host capacity; CPU cores and bytes retain absolute usage."""

    timestamp: float
    scope: str
    target: str
    cpu_percent: float | None
    memory_percent: float
    cpu_cores: float | None
    memory_bytes: int


@dataclass(kw_only=True)
class ResourceSummary:
    scope: str
    target: str
    metric: str
    unit: str
    samples: int
    min_value: float
    avg_value: float
    p95_value: float
    p99_value: float
    max_value: float


@dataclass(kw_only=True)
class BatchRunResult:
    """An outcome exists even when service startup or job submission fails."""

    environment: Environment
    file_type: FileType
    output_dir: Path
    started_at: str
    status: str = "Failed"
    job_run_id: str | None = None
    error: str = ""
    ended_at: str = ""
    case_duration_seconds: float | None = None
    measurement_started_at: float | None = None
    measurement_ended_at: float | None = None
    job_stats: JobRunStats | None = None
    resources: list[ResourceSummary] = field(default_factory=list)
    diagnostic_errors: list[str] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)
    configuration: dict = field(default_factory=dict)
