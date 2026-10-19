#!/usr/bin/env python3
"""Collect a finished batching run's logs, API response, and CSV report."""

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import UUID

BATCHING_DIR = Path(__file__).resolve().parents[1]
TERMINAL_STATUSES = {
    "Completed", "CompletedWithErrors", "CompletedWithWarnings", "Failed",
    "Canceled", "Cancelled", "Aborted",
}
CASES = ("cpu-unlimited", "cpu-limited", "gpu-unlimited", "gpu-limited")
FIELDS = [
    "collection_id", "case", "input_type", "flow_name", "job_id", "job_run_id", "status",
    "duration_seconds", "completed_docs", "total_docs", "failed_docs",
    "skipped_docs", "node_timings_seconds", "artifacts_dir", "collection_errors",
]


def download(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:
        return response.read()


def collect_container_logs(*, case: str, stats: dict, destination: Path) -> None:
    """Keep the full run time window, including messages without a run ID."""
    compose_dir = BATCHING_DIR / "with_limits" if case.endswith("-limited") else BATCHING_DIR
    hardware = case.split("-")[0]
    command = [
        "docker", "compose", "--env-file", str(BATCHING_DIR.parent / ".env"),
        "-f", str(compose_dir / f"docker-compose-{hardware}.yml"),
        "-f", str(compose_dir / "docker-compose-infra.yml"), "ps", "-q", "docpipe",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    containers = result.stdout.split()
    if result.returncode or len(containers) != 1:
        raise RuntimeError(result.stderr.strip() or "Expected one running docpipe container; collect before stopping it")
    start = int(stats.get("start_time") or 0)
    if not start:
        raise RuntimeError("Run start_time is unavailable; cannot bound container logs")
    end = int(stats.get("end_time") or time.time())
    with destination.open("wb") as output:
        result = subprocess.run(
            ["docker", "logs", "--since", str(start - 1), "--until", str(end + 2), containers[0]],
            stdout=output, stderr=subprocess.STDOUT, timeout=60,
        )
    if result.returncode:
        raise RuntimeError(f"docker logs exited with {result.returncode}; see {destination}")


def append_results(*, destination: Path, row: dict) -> None:
    """Append every collection without rewriting previous rows."""
    with destination.open("a+", newline="", encoding="utf-8") as output:
        output.seek(0)
        header = next(csv.reader(output), None)
        if header is not None and header != FIELDS:
            raise RuntimeError(f"Unexpected CSV header in {destination}; existing results were preserved")
        output.seek(0, 2)
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        if header is None:
            writer.writeheader()
        writer.writerow(row)


def collect(args: argparse.Namespace) -> int:
    run_id = str(args.job_run_id)
    url = f"{args.api_url.rstrip('/')}/api/v1/job_runs/{run_id}"
    payload = json.loads(download(f"{url}?include_logs=true"))
    stats = payload["job_stats"]
    if stats["job_run_id"] != run_id:
        raise RuntimeError("API returned a different job_run_id")
    if stats["status"] not in TERMINAL_STATUSES:
        raise RuntimeError(f"Run is {stats['status']}; wait for completion before collecting")
    if not args.flow_name or args.flow_name in {".", ".."} or any(c in args.flow_name for c in "/\\\0"):
        raise RuntimeError("flow-name must be a single folder name")

    results_dir = args.results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    while True:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        collection_id = f"{args.flow_name}_{timestamp}"
        run_dir = results_dir / collection_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        break
    (run_dir / "job_stats.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    node_logs = [payload[node_id] for node_id in payload.get("node_sequence", []) if isinstance(payload.get(node_id), str)]
    (run_dir / "node_execution.log").write_text(
        f"Flow: {args.flow_name}\nJob run ID: {run_id}\n\n" + "\n\n".join(node_logs) + "\n",
        encoding="utf-8",
    )

    errors = []
    try:
        (run_dir / "summary.csv").write_bytes(download(f"{url}/report"))
    except (RuntimeError, OSError) as error:
        errors.append(f"summary.csv: {error}")
    try:
        collect_container_logs(case=args.case, stats=stats, destination=run_dir / "flow_run.log")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        errors.append(f"flow_run.log: {error}")

    nodes = {node["name"]: node.get("time_taken", 0) for node in stats.get("node_stats", {}).values()}
    row = {
        "collection_id": collection_id, "case": args.case, "input_type": args.input_type, "flow_name": args.flow_name,
        "job_id": stats["job_id"], "job_run_id": run_id, "status": stats["status"],
        "duration_seconds": stats.get("duration", 0), "completed_docs": stats.get("completed_docs", 0),
        "total_docs": stats.get("total_docs", 0), "failed_docs": stats.get("failed_docs", 0),
        "skipped_docs": stats.get("skipped_docs", 0), "node_timings_seconds": json.dumps(nodes, sort_keys=True),
        "artifacts_dir": str(run_dir.relative_to(results_dir)), "collection_errors": " | ".join(errors),
    }
    append_results(destination=results_dir / "results.csv", row=row)
    print(f"{args.case} / {args.input_type}: {stats['status']}, {stats.get('duration', 0)} seconds")
    print(f"Artifacts: {run_dir}\nResults: {results_dir / 'results.csv'}")
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-run-id", required=True, type=UUID)
    parser.add_argument("--flow-name", required=True)
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--input-type", required=True, choices=("pdf", "text"))
    parser.add_argument("--api-url", default="http://localhost:8080")
    parser.add_argument("--results-dir", type=Path, default=BATCHING_DIR / "results")
    try:
        return collect(parser.parse_args())
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(f"Collection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
