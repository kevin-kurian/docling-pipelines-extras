"""Collect and parse job statistics used by performance reports."""

import json
import os
from pathlib import Path
from urllib.request import urlopen

from .models import Environment, FileType, JobRunStats


class JobStatsCollector:
    def __init__(self, *, job_run_id: str, output_dir: Path) -> None:
        base_url = os.environ["DOCPIPE_API_BASE_URL"].rstrip("/")
        self.url = f"{base_url}/api/v1/job_runs/{job_run_id}"
        self.output_dir = output_dir / "_stats"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _download_job_stats(self) -> Path:
        with urlopen(self.url, timeout=30) as response:
            payload = json.load(response)
        path = self.output_dir / "job_stats.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _parse_stats(*, payload: dict, environment: Environment, file_type: FileType) -> JobRunStats:
        stats = payload["job_stats"]
        duration = float(stats["duration"]) if stats.get("duration") is not None else None
        nodes = stats.get("node_stats") or {}
        timings = {
            node.get("name", node_id): float(node["time_taken"])
            for node_id, node in nodes.items()
            if node.get("time_taken") is not None
        }
        # A job duration is final only for terminal jobs. Running snapshots often contain zero.
        terminal = stats.get("status") in {
            "Completed",
            "CompletedWithWarnings",
            "CompletedWithErrors",
            "Failed",
            "Canceled",
            "Aborted",
        }
        duration = duration if terminal else None
        completed = stats.get("completed_docs")
        pages = stats.get("total_pages_processed")
        chunk_counts = [
            node.get("node_metadata", {}).get("node_metadata", {}).get("total_chunks") for node in nodes.values()
        ]
        chunks = [count for count in chunk_counts if count is not None]
        return JobRunStats(
            environment=environment,
            file_type=file_type,
            status=stats.get("status", "Unknown"),
            total_execution_time_seconds=duration,
            total_docs=stats.get("total_docs"),
            processed_docs=stats.get("processed_docs"),
            completed_docs=completed,
            failed_docs=stats.get("failed_docs"),
            skipped_docs=stats.get("skipped_docs"),
            total_pages_processed=pages,
            total_chunks=sum(chunks) if chunks else None,
            documents_per_second=completed / duration if duration and completed is not None else None,
            pages_per_second=pages / duration if duration and pages is not None else None,
            operator_timings_seconds=timings,
        )

    def build_job_run_stats(self, *, environment: Environment, file_type: FileType) -> JobRunStats:
        """Download the statistics used by the performance reports."""
        stats_path = self._download_job_stats()
        return self._parse_stats(
            payload=json.loads(stats_path.read_text(encoding="utf-8")),
            environment=environment,
            file_type=file_type,
        )
