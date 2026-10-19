#!/usr/bin/env python3
"""Run flow files and record host, Colima, and container CPU/memory usage."""

import argparse
import csv
import json
import os
import signal
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

import docker
import psutil
from matplotlib.figure import Figure

from collect_results import FIELDS, TERMINAL_STATUSES, append_results, write_report

SAMPLE_FIELDS = [
    "collection_id", "job_run_id", "timestamp", "phase", "scope", "target",
    "sample_seconds", "cpu_cores", "cpu_percent", "memory_used_bytes", "memory_limit_bytes",
]
VM_SAMPLER = """
import json, os, sys, time
interval = float(sys.argv[1])
def read():
    with open('/proc/stat') as source:
        cpu = list(map(int, source.readline().split()[1:9]))
    with open('/proc/meminfo') as source:
        memory = {key.rstrip(':'): int(value) * 1024
                  for key, value, *_ in map(str.split, source)}
    return cpu, memory
previous, _ = read()
last = time.monotonic()
while True:
    time.sleep(interval)
    current, memory = read()
    now = time.monotonic()
    total = sum(current) - sum(previous)
    idle = current[3] + current[4] - previous[3] - previous[4]
    print(json.dumps({
        'timestamp': time.time(), 'sample_seconds': now - last,
        'cpu_cores': (total - idle) / total * os.cpu_count(),
        'memory_used_bytes': memory['MemTotal'] - memory['MemAvailable'],
        'memory_limit_bytes': memory['MemTotal'],
    }), flush=True)
    previous, last = current, now
"""


def request(*, args: argparse.Namespace, path: str, payload: dict | None = None) -> bytes:
    data = json.dumps(payload).encode() if payload is not None else None
    with urlopen(Request(args.api_url.rstrip("/") + path, data=data,
                         headers={"Content-Type": "application/json"}), timeout=30) as response:
        return response.read()


def compose_command(args: argparse.Namespace) -> list[str]:
    command = ["docker", "compose"]
    if args.env_file:
        command += ["--env-file", str(args.env_file)]
    if args.project_name:
        command += ["--project-name", args.project_name]
    for path in args.compose_file:
        command += ["-f", str(path)]
    return command


class Sampler:
    def __init__(self, *, args: argparse.Namespace, client, containers: list, run_dir: Path):
        self.args, self.client, self.containers = args, client, containers
        self.run_dir = run_dir
        self.phase, self.job_run_id = "before", ""
        self.stop_event, self.ready = threading.Event(), threading.Event()
        self.processes = {}
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.output = (run_dir / "_" / "resource_samples.csv").open("w", newline="")
        self.writer = csv.DictWriter(self.output, fieldnames=SAMPLE_FIELDS)
        self.writer.writeheader()

    def start(self):
        for profile in self.args.colima_profile:
            self.processes[profile] = subprocess.Popen(
                ["colima", "ssh", "--profile", profile, "--", "python3", "-u", "-c", VM_SAMPLER, str(self.args.interval)],
                stdout=subprocess.PIPE, text=True, start_new_session=True,
            )
        self.future = self.pool.submit(self._sample)
        deadline = time.monotonic() + self.args.interval + 30
        while not self.ready.wait(0.1):
            self.check()
            if time.monotonic() > deadline:
                raise TimeoutError("Resource samplers did not produce a sample")

    def check(self):
        if self.future.done():
            self.future.result()

    def wait(self, seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        self.check()

    def _sample(self):
        previous = {c.id: self.client.api.stats(c.id, stream=False, one_shot=True) for c in self.containers}
        psutil.cpu_percent()
        last = time.monotonic()
        while not self.stop_event.wait(max(0, self.args.interval - (time.monotonic() - last))):
            rows = []
            for profile, process in self.processes.items():
                line = process.stdout.readline()
                if self.stop_event.is_set():
                    return
                rows.append({"scope": "colima", "target": profile, **json.loads(line)})
            now = time.monotonic()
            memory = psutil.virtual_memory()
            rows.append({
                "scope": "host", "target": "host", "sample_seconds": now - last,
                "cpu_cores": psutil.cpu_percent() / 100 * psutil.cpu_count(),
                "memory_used_bytes": memory.total - memory.available, "memory_limit_bytes": memory.total,
            })
            for container in self.containers:
                current = self.client.api.stats(container.id, stream=False, one_shot=True)
                cpu, old = current["cpu_stats"], previous[container.id]["cpu_stats"]
                cores = ((cpu["cpu_usage"]["total_usage"] - old["cpu_usage"]["total_usage"])
                         / (cpu["system_cpu_usage"] - old["system_cpu_usage"]) * cpu["online_cpus"])
                memory = current["memory_stats"]
                cache = memory["stats"].get("inactive_file", memory["stats"].get("total_inactive_file", 0))
                rows.append({
                    "scope": "container", "target": container.name, "sample_seconds": now - last,
                    "cpu_cores": cores, "memory_used_bytes": memory["usage"] - cache if cache < memory["usage"] else memory["usage"],
                    "memory_limit_bytes": memory["limit"],
                })
                previous[container.id] = current
            phase, job_run_id = self.phase, self.job_run_id
            for row in rows:
                timestamp = row.pop("timestamp", time.time())
                self.writer.writerow({
                    "collection_id": self.run_dir.name, "job_run_id": job_run_id,
                    "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat(), "phase": phase,
                    "cpu_percent": row["cpu_cores"] * 100, **row,
                })
            self.output.flush()
            self.ready.set()
            last = now

    def close(self):
        self.stop_event.set()
        for process in self.processes.values():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        try:
            if self.future is not None:
                self.future.result()
        finally:
            self.pool.shutdown()
            self.output.close()
            for process in self.processes.values():
                process.stdout.close()


def plot_samples(run_dir: Path):
    series = defaultdict(list)
    with (run_dir / "_" / "resource_samples.csv").open() as source:
        for row in csv.DictReader(source):
            series[row["scope"], row["target"]].append(row)
    origin = min(datetime.fromisoformat(row["timestamp"]) for rows in series.values() for row in rows)
    figure = Figure(figsize=(12, 2.8 * len(series)), layout="constrained")
    axes = figure.subplots(len(series), 2, sharex=True, squeeze=False)
    for ((scope, target), rows), pair in zip(series.items(), axes):
        rows.sort(key=lambda row: row["timestamp"])
        seconds = [(datetime.fromisoformat(row["timestamp"]) - origin).total_seconds() for row in rows]
        phases = [(seconds[i], row["phase"]) for i, row in enumerate(rows)
                  if i == 0 or row["phase"] != rows[i - 1]["phase"]]
        for axis, field, scale, label in zip(pair, ("cpu_cores", "memory_used_bytes"),
                                             (1, 1024 ** 2), ("CPU (cores)", "Memory (MiB)")):
            values = [float(row[field]) / scale for row in rows]
            axis.plot(seconds, values, marker=".", markersize=3)
            axis.set(title=f"{scope}: {target}", ylabel=label, xlabel="Time since first sample (s)")
            axis.set_ylim(0, max(values) * 1.15 or 1)
            axis.tick_params(labelbottom=True)
            axis.ticklabel_format(axis="y", style="plain", useOffset=False)
            axis.grid(alpha=0.25)
            for start, phase in phases:
                axis.axvline(start, color="gray", linestyle="--", linewidth=0.6)
                axis.text(start, 0.98, phase, transform=axis.get_xaxis_transform(), va="top", fontsize=8)
    figure.savefig(run_dir / "_" / "resource_samples.png", dpi=150, bbox_inches="tight")


def summarize(*, run_dir: Path, job_run_id: str):
    groups = defaultdict(list)
    with (run_dir / "_" / "resource_samples.csv").open() as source:
        for row in csv.DictReader(source):
            groups[row["scope"], row["target"], row["phase"]].append(row)
    summaries = []
    for (scope, target, phase), rows in groups.items():
        seconds = sum(float(row["sample_seconds"]) for row in rows)
        summary = {"collection_id": run_dir.name, "job_run_id": job_run_id,
                   "scope": scope, "target": target, "phase": phase, "samples": len(rows)}
        for field in ("cpu_cores", "memory_used_bytes"):
            summary[field + "_avg"] = sum(float(row[field]) * float(row["sample_seconds"]) for row in rows) / seconds
            summary[field + "_max"] = max(float(row[field]) for row in rows)
        summaries.append(summary)
    baselines = {(row["scope"], row["target"]): row["memory_used_bytes_avg"] for row in summaries if row["phase"] == "before"}
    for row in summaries:
        row["memory_delta_from_baseline_bytes"] = row["memory_used_bytes_avg"] - baselines[row["scope"], row["target"]]
    with (run_dir / "_" / "resource_summary.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(summaries[0]) if summaries else ["collection_id"])
        writer.writeheader()
        writer.writerows(summaries)
    return groups


def collect(*, args: argparse.Namespace, run_dir: Path, run_id: str, input_type: str, containers: list):
    path = f"/api/v1/job_runs/{run_id}"
    payload = json.loads(request(args=args, path=path + "?include_logs=true"))
    stats = payload["job_stats"]
    raw_dir = run_dir / "_"
    (raw_dir / "job_stats.json").write_text(json.dumps(payload, indent=2) + "\n")
    write_report(run_dir=run_dir)
    (raw_dir / "summary.csv").write_bytes(request(args=args, path=path + "/report"))
    logs = [payload[node] for node in payload.get("node_sequence", []) if isinstance(payload.get(node), str)]
    (raw_dir / "node_execution.log").write_text("\n\n".join(logs) + "\n")
    with (raw_dir / "flow_run.log").open("wb") as output:
        for container in containers:
            output.write(f"\n=== {container.name} ===\n".encode())
            output.flush()
            subprocess.run(
                ["docker", "logs", "--since", str(stats["start_time"] - 1), "--until", str(stats["end_time"] + 2), container.id],
                stdout=output, stderr=subprocess.STDOUT, timeout=30, check=True,
            )
    row = dict.fromkeys(FIELDS, "")
    row.update(collection_id=run_dir.name, case=args.case, input_type=input_type,
               flow_name=run_dir.name, job_id=stats["job_id"], job_run_id=run_id, status=stats["status"],
               duration_seconds=stats["duration"], artifacts_dir=run_dir.name,
               node_timings_seconds=json.dumps({node["name"]: node["time_taken"] for node in stats.get("node_stats", {}).values()}))
    row.update({key: stats.get(key, 0) for key in ("completed_docs", "total_docs", "failed_docs", "skipped_docs")})
    (args.results_dir / "_").mkdir(exist_ok=True)
    append_results(destination=args.results_dir / "_" / "results.csv", row=row)
    print(f"Report: {run_dir / 'REPORT.md'}", flush=True)


def run_flow(*, args: argparse.Namespace, flow_file: Path, client, containers: list):
    flow = json.loads(flow_file.read_text())
    input_type = flow_file.stem.removeprefix("sample_flow_")
    name = f"{input_type}_{args.case}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    flow["flow_name"] = name
    run_dir = args.results_dir / name
    (run_dir / "_").mkdir(parents=True)
    flow_id = json.loads(request(args=args, path="/api/v1/flows", payload=flow))["flow_id"]
    metadata = {"collection_id": name, "flow_file": str(flow_file), "input_type": input_type,
                "case": args.case, "flow_id": flow_id}
    sampler = Sampler(args=args, client=client, containers=containers, run_dir=run_dir)
    print(run_dir, flush=True)
    try:
        sampler.start()
        sampler.wait(args.before_seconds)
        sampler.phase = "during"
        job = json.loads(args.job_config.read_text()) if args.job_config else {}
        job.setdefault("entity", {}).setdefault("job", {})["asset_ref"] = flow_id
        run_id = json.loads(request(args=args, path="/api/v1/job_runs", payload=job))["job_run_id"]
        metadata["job_run_id"] = sampler.job_run_id = run_id
        deadline = time.monotonic() + args.timeout_seconds
        while True:
            sampler.check()
            payload = json.loads(request(args=args, path=f"/api/v1/job_runs/{run_id}"))
            status = payload["job_stats"]["status"]
            if status in TERMINAL_STATUSES:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Job run {run_id} exceeded {args.timeout_seconds}s")
            sampler.wait(args.interval)
        metadata["job_status"] = status
        sampler.phase = "after"
        sampler.wait(args.after_seconds)
    finally:
        (run_dir / "_" / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        sampler.close()
        plot_samples(run_dir)
        groups = summarize(run_dir=run_dir, job_run_id=metadata.get("job_run_id", ""))
    collect(args=args, run_dir=run_dir, run_id=run_id, input_type=input_type, containers=containers)
    if {phase for _, _, phase in groups} != {"before", "during", "after"}:
        raise RuntimeError("Missing a measurement phase; reduce --interval for short jobs")
    if status not in {"Completed", "CompletedWithWarnings"}:
        raise RuntimeError(f"Job run {run_id}: {status}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow-file", type=Path, action="extend", nargs="+", required=True)
    parser.add_argument("--compose-file", type=Path, action="append", required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project-name")
    parser.add_argument("--service", action="append", default=[])
    parser.add_argument("--case", required=True)
    parser.add_argument("--job-config", type=Path)
    parser.add_argument("--api-url", default="http://localhost:8080")
    parser.add_argument("--results-dir", type=Path, default=Path(__file__).resolve().parents[1] / "results")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--colima-profile", action="append")
    parser.add_argument("--skip-colima", action="store_true")
    for name, default in (("interval", 1), ("before-seconds", 30), ("after-seconds", 30), ("timeout-seconds", 3600)):
        parser.add_argument(f"--{name}", type=float, default=default)
    args = parser.parse_args()
    if args.interval <= 0 or min(args.before_seconds, args.after_seconds) < args.interval or args.repeat < 1:
        parser.error("Use a positive interval/repeat and before/after windows of at least one interval")
    args.colima_profile = [] if args.skip_colima else args.colima_profile or ["default"]
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
        client = docker.from_env(timeout=10)
    else:
        context = json.loads(subprocess.check_output(["docker", "context", "inspect"], text=True))[0]
        client = docker.DockerClient(base_url=context["Endpoints"]["docker"]["Host"], timeout=10)
    try:
        identifiers = subprocess.check_output(compose_command(args) + ["ps", "--all", "-q", *args.service], text=True).split()
        if not identifiers:
            raise RuntimeError("No containers found; start the Compose stack first")
        containers = [client.containers.get(identifier) for identifier in identifiers]
        for _ in range(args.repeat):
            for flow_file in args.flow_file:
                run_flow(args=args, flow_file=flow_file, client=client, containers=containers)
    finally:
        client.close()


if __name__ == "__main__":
    main()
