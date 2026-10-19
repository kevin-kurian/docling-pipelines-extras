#!/usr/bin/env python3
"""Collect one finished run into REPORT.md with supporting files under _."""

import argparse
import csv
import json
import os
import shutil
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
CASES = ("cpu", "gpu", "mps")
FIELDS = [
    "collection_id", "case", "input_type", "flow_name", "job_id", "job_run_id", "status",
    "duration_seconds", "completed_docs", "total_docs", "failed_docs",
    "skipped_docs", "node_timings_seconds", "artifacts_dir", "collection_errors",
]


def download(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:
        return response.read()


def collect_container_logs(*, case: str, destination: Path) -> None:
    """Collect the complete docpipe container log before stopping the stack."""
    compose_case = "cpu" if case == "mps" else case
    command = [
        "docker", "compose", "--env-file", str(BATCHING_DIR / ".env"),
        "-f", str(BATCHING_DIR / f"docker-compose-{compose_case}.yml"),
        "-f", str(BATCHING_DIR / "docker-compose-infra.yml"), "ps", "-q", "docpipe",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    containers = result.stdout.split()
    if result.returncode or len(containers) != 1:
        raise RuntimeError(result.stderr.strip() or "Expected one running docpipe container; collect before stopping it")
    with destination.open("wb") as output:
        result = subprocess.run(
            ["docker", "logs", containers[0]],
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


def create_collection_directory(*, results_dir: Path, input_type: str, case: str) -> tuple[str, Path]:
    """Create a uniquely named report directory with sortable UTC timestamp first."""
    results_dir.mkdir(parents=True, exist_ok=True)
    while True:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        collection_id = f"{timestamp}_{case}_{input_type}"
        run_dir = results_dir / collection_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            time.sleep(0.05)
            continue
        return collection_id, run_dir


def collect_native_logs(*, source: Path, destination: Path) -> None:
    """Copy native inference service logs into a collected run."""
    for name in ("docling-serve.log", "ollama.log"):
        shutil.copyfile(source / name, destination / name)


def write_report(*, run_dir: Path, notes: tuple[str, ...] = ()) -> None:
    raw_dir = run_dir / "_"
    payload = json.loads((raw_dir / "job_stats.json").read_text())
    stats = payload["job_stats"]
    metadata_path = raw_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    case = metadata.get("case", "")
    input_type = metadata.get("input_type", Path(metadata.get("flow_file", "")).stem.removeprefix("sample_flow_"))
    lines = [
        f"# {input_type.upper()} / {case}", "",
        f"- Status: {stats['status']}",
        f"- Duration: {stats.get('duration', 0)} seconds",
        f"- Documents: {stats.get('completed_docs', 0)}/{stats.get('total_docs', 0)} completed; "
        f"{stats.get('failed_docs', 0)} failed; {stats.get('skipped_docs', 0)} skipped",
        f"- Reported pages: {stats.get('total_pages_processed', 'unavailable')}",
        f"- Job run: `{stats['job_run_id']}`",
    ]
    if stats.get("start_time"):
        started = datetime.fromtimestamp(stats["start_time"], UTC).isoformat()
        lines.append(f"- Started: {started} (UTC)")
    lines += ["", *notes, "", "## Operator timings", "",
              "| Operator | Status | Seconds |", "|---|---|---:|"]
    for node in stats.get("node_stats", {}).values():
        lines.append(f"| {node['name']} | {node.get('node_status', '')} | {node.get('time_taken', 0)} |")
    resource_path = raw_dir / "resource_summary.csv"
    if resource_path.exists():
        lines += ["", "## Resources during execution", "",
                  "CPU is in cores; memory is in GiB. Host, VM, and container figures overlap.", "",
                  "| Scope | Target | Avg CPU | Peak CPU | Avg memory | Peak memory |",
                  "|---|---|---:|---:|---:|---:|"]
        with resource_path.open() as source:
            for row in csv.DictReader(source):
                if row["phase"] != "during":
                    continue
                lines.append(
                    f"| {row['scope']} | {row['target']} | {float(row['cpu_cores_avg']):.2f} | "
                    f"{float(row['cpu_cores_max']):.2f} | {float(row['memory_used_bytes_avg']) / 1024 ** 3:.2f} | "
                    f"{float(row['memory_used_bytes_max']) / 1024 ** 3:.2f} |"
                )
    if (raw_dir / "resource_samples.png").exists():
        lines += ["", "## Resource timeline", "", "![CPU and memory across the run lifecycle](_/resource_samples.png)"]
    if metadata.get("collection_errors"):
        lines += ["", f"Collection errors: {metadata['collection_errors']}"]
    lines += ["", "Supporting data and logs: [_](_/).", ""]
    (run_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


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
    collection_id, run_dir = create_collection_directory(
        results_dir=results_dir, input_type=args.input_type, case=args.case
    )
    raw_dir = run_dir / "_"
    raw_dir.mkdir()
    (raw_dir / "job_stats.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    node_logs = [payload[node_id] for node_id in payload.get("node_sequence", []) if isinstance(payload.get(node_id), str)]
    (raw_dir / "node_execution.log").write_text(
        f"Flow: {args.flow_name}\nJob run ID: {run_id}\n\n" + "\n\n".join(node_logs) + "\n",
        encoding="utf-8",
    )

    errors = []
    try:
        (raw_dir / "summary.csv").write_bytes(download(f"{url}/report"))
    except (RuntimeError, OSError) as error:
        errors.append(f"summary.csv: {error}")
    try:
        collect_container_logs(case=args.case, destination=raw_dir / "flow_run.log")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        errors.append(f"flow_run.log: {error}")
    if args.case == "mps" and args.native_logs_dir is not None:
        try:
            collect_native_logs(source=args.native_logs_dir, destination=raw_dir)
        except OSError as error:
            errors.append(f"native logs: {error}")

    nodes = {node["name"]: node.get("time_taken", 0) for node in stats.get("node_stats", {}).values()}
    row = {
        "collection_id": collection_id, "case": args.case, "input_type": args.input_type, "flow_name": args.flow_name,
        "job_id": stats["job_id"], "job_run_id": run_id, "status": stats["status"],
        "duration_seconds": stats.get("duration", 0), "completed_docs": stats.get("completed_docs", 0),
        "total_docs": stats.get("total_docs", 0), "failed_docs": stats.get("failed_docs", 0),
        "skipped_docs": stats.get("skipped_docs", 0), "node_timings_seconds": json.dumps(nodes, sort_keys=True),
        "artifacts_dir": str(run_dir.relative_to(results_dir)), "collection_errors": " | ".join(errors),
    }
    metadata = {"case": args.case, "input_type": args.input_type, "flow_name": args.flow_name,
                "job_run_id": run_id, "collection_errors": row["collection_errors"]}
    (raw_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    write_report(run_dir=run_dir)
    (results_dir / "_").mkdir(exist_ok=True)
    append_results(destination=results_dir / "_" / "results.csv", row=row)
    print(f"{args.case} / {args.input_type}: {stats['status']}, {stats.get('duration', 0)} seconds")
    print(f"Report: {run_dir / 'REPORT.md'}")
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-run-id", required=True, type=UUID)
    parser.add_argument("--flow-name", required=True)
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--input-type", required=True, choices=("pdf_ocr", "pdf_non_ocr", "text"))
    parser.add_argument("--api-url", default="http://localhost:8080")
    parser.add_argument("--results-dir", type=Path, default=BATCHING_DIR / "results")
    parser.add_argument(
        "--native-logs-dir", type=Path, default=os.environ.get("MPS_LOG_DIR"),
        help="Native service log directory (default: MPS_LOG_DIR)",
    )
    try:
        return collect(parser.parse_args())
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(f"Collection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
