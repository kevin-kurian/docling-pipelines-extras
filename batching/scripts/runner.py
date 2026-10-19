#!/usr/bin/env python3
"""Run one flow in a fresh Compose environment and collect all run artifacts."""

import argparse
import csv
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import docker
import psutil
from matplotlib.figure import Figure

from collect_results import (
    CASES,
    FIELDS,
    TERMINAL_STATUSES,
    append_results,
    create_collection_directory,
    write_report,
)

BATCHING_DIR = Path(__file__).resolve().parents[1]
INFRA_SERVICES = ("docling-serve", "ollama", "opensearch")
MPS_SERVICE_STARTUP_TIMEOUT_SECONDS = 900
MPS_SERVICE_POLL_INTERVAL_SECONDS = 2
MPS_SERVICE_SHUTDOWN_TIMEOUT_SECONDS = 30

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
                         headers={"Content-Type": "application/json"}), timeout=60) as response:
        return response.read()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow-file", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, help="Default: infer CPU/GPU from --compose-file")
    parser.add_argument(
        "--infra-services", nargs="+", choices=INFRA_SERVICES,
        help="Infrastructure to start (default: all; MPS: opensearch only)",
    )
    parser.add_argument(
        "--native-logs-dir", type=Path, default=os.environ.get("MPS_LOG_DIR"),
        help="Native service log directory (default: MPS_LOG_DIR or the run report directory)",
    )
    parser.add_argument("--env-file", type=Path, default=BATCHING_DIR / ".env")
    parser.add_argument("--project-name")
    parser.add_argument("--api-url", default="http://localhost:8080")
    parser.add_argument("--results-dir", type=Path, default=BATCHING_DIR / "results")
    parser.add_argument("--colima-profile", action="append")
    parser.add_argument("--skip-colima", action="store_true")
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args()
    args.flow_file = args.flow_file.resolve()
    args.compose_file = args.compose_file.resolve()
    args.infra_file = args.compose_file.with_name("docker-compose-infra.yml")
    for path in (args.flow_file, args.compose_file, args.infra_file):
        if not path.is_file():
            parser.error(f"File not found: {path}")
    if args.compose_file == args.infra_file:
        parser.error("--compose-file must select the CPU or GPU application stack")
    if not 0 < args.interval < float("inf") or not 0 < args.timeout_seconds < float("inf"):
        parser.error("Use a positive finite sampling interval and timeout")
    compose_case = args.compose_file.stem.removeprefix("docker-compose-")
    args.case = args.case or compose_case
    if args.case not in CASES or compose_case != ("cpu" if args.case == "mps" else args.case):
        parser.error("Use the matching CPU/GPU Compose file; MPS uses docker-compose-cpu.yml")
    default_services = ("opensearch",) if args.case == "mps" else INFRA_SERVICES
    args.infra_services = list(dict.fromkeys(args.infra_services or default_services))
    if args.case == "mps" and args.infra_services != ["opensearch"]:
        parser.error("MPS uses native inference services; --infra-services must be opensearch")
    args.project_name = args.project_name or args.compose_file.parent.name
    return args


class Sampler:
    """Sample host/VM usage and discover containers as they start and stop."""

    def __init__(self, *, args: argparse.Namespace, client, run_dir: Path):
        self.args, self.client, self.run_dir = args, client, run_dir
        self.phase, self.job_run_id = "startup", ""
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
        if self.future is not None and self.future.done():
            self.future.result()

    def wait(self, seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        self.check()

    def _sample(self):
        previous = {}
        psutil.cpu_percent()
        last = time.monotonic()
        while not self.stop_event.wait(max(0, self.args.interval - (time.monotonic() - last))):
            rows = []
            for profile, process in self.processes.items():
                line = process.stdout.readline()
                if self.stop_event.is_set():
                    return
                if not line:
                    raise RuntimeError(f"Colima sampler exited: {profile}")
                rows.append({"scope": "colima", "target": profile, **json.loads(line)})
            now = time.monotonic()
            memory = psutil.virtual_memory()
            rows.append({
                "scope": "host", "target": "host", "sample_seconds": now - last,
                "cpu_cores": psutil.cpu_percent() / 100 * psutil.cpu_count(),
                "memory_used_bytes": memory.total - memory.available, "memory_limit_bytes": memory.total,
            })
            containers = self.client.containers.list(
                filters={"label": f"com.docker.compose.project={self.args.project_name}"},
            )
            current_ids = {container.id for container in containers}
            previous = {key: value for key, value in previous.items() if key in current_ids}
            for container in containers:
                try:
                    current = self.client.api.stats(container.id, stream=False, one_shot=True)
                except docker.errors.APIError as error:
                    if error.status_code in {404, 409} or "not running" in str(error).lower():
                        continue
                    raise
                cpu = current.get("cpu_stats", {})
                memory = current.get("memory_stats", {})
                if not cpu.get("system_cpu_usage") or "usage" not in memory:
                    continue
                old, sampled_at = previous.get(container.id, (current, last))
                system_delta = cpu["system_cpu_usage"] - old["cpu_stats"]["system_cpu_usage"]
                usage_delta = cpu["cpu_usage"]["total_usage"] - old["cpu_stats"]["cpu_usage"]["total_usage"]
                cores = max(0, usage_delta / system_delta * cpu.get("online_cpus", 1)) if system_delta > 0 else 0
                cache = memory.get("stats", {}).get("inactive_file", memory.get("stats", {}).get("total_inactive_file", 0))
                rows.append({
                    "scope": "container", "target": container.name, "sample_seconds": now - sampled_at,
                    "cpu_cores": cores, "memory_used_bytes": max(0, memory["usage"] - cache),
                    "memory_limit_bytes": memory["limit"],
                })
                previous[container.id] = current, now
            phase, job_run_id = self.phase, self.job_run_id
            for row in rows:
                timestamp = row.pop("timestamp", time.time())
                self.writer.writerow({
                    "collection_id": self.run_dir.name, "job_run_id": job_run_id,
                    "timestamp": datetime.fromtimestamp(timestamp).astimezone().isoformat(), "phase": phase,
                    "cpu_percent": row["cpu_cores"] * 100, **row,
                })
            self.output.flush()
            self.ready.set()
            last = now

    def close(self):
        self.stop_event.set()
        try:
            for process in self.processes.values():
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        finally:
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
    if not series:
        return
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
    baselines = {(row["scope"], row["target"]): row["memory_used_bytes_avg"] for row in summaries if row["phase"] == "startup"}
    for row in summaries:
        row["memory_delta_from_baseline_bytes"] = (
            row["memory_used_bytes_avg"] - baselines[row["scope"], row["target"]]
            if (row["scope"], row["target"]) in baselines else ""
        )
    with (run_dir / "_" / "resource_summary.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(summaries[0]) if summaries else ["collection_id"])
        writer.writeheader()
        writer.writerows(summaries)
    return groups


class Runner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.flow = json.loads(args.flow_file.read_text())
        self.embedding_models = self._find_embedding_models(self.flow)
        self.input_type = args.flow_file.stem.removeprefix("sample_flow_")
        self.flow_name = self.flow["flow_name"]
        if args.flow_file.stem.startswith("sample_flow_"):
            timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            self.flow_name = f"sample_flow_{args.case}_{self.input_type}_{timestamp}"
            self.flow["flow_name"] = self.flow_name
        collection_id, self.run_dir = create_collection_directory(
            results_dir=args.results_dir, input_type=self.input_type, case=args.case
        )
        self.raw_dir = self.run_dir / "_"
        self.raw_dir.mkdir(parents=True)
        self.native_logs_dir = Path(args.native_logs_dir) if args.native_logs_dir else self.raw_dir
        self.native_processes: dict[str, subprocess.Popen[bytes]] = {}
        self.client = self.sampler = None
        self.stack_started = False
        self.job_run_id = ""
        self.payload = None
        self.errors = []
        self.metadata = {
            "collection_id": collection_id, "flow_file": str(args.flow_file),
            "flow_name": self.flow_name, "input_type": self.input_type, "case": args.case,
            "compose_file": str(args.compose_file), "infra_file": str(args.infra_file),
            "project_name": args.project_name, "infra_services": args.infra_services,
        }
        (self.raw_dir / "flow.json").write_text(json.dumps(self.flow, indent=2) + "\n")
        print(self.run_dir, flush=True)

    @staticmethod
    def _find_embedding_models(flow: dict) -> list[str]:
        models = set()
        for node in flow["flow"]:
            if node["type"] != "embeddings":
                continue
            config = node.get("config", {})
            provider = config.get("provider_config", {})
            endpoint = urlsplit(provider.get("api_base", ""))
            if config.get("provider") == "ollama" or endpoint.hostname == "ollama" or endpoint.port == 11434:
                model = provider.get("model_id") or provider.get("model")
                if not model:
                    raise ValueError(f"Missing embedding model for {node['name']}")
                models.add(model.removeprefix("openai/"))
        return sorted(models)

    def start_native_services(self):
        if self.args.case != "mps":
            return

        services = (
            ("Ollama", "ollama", 11434, ["ollama", "serve"], "http://localhost:11434/api/tags",
             {"OLLAMA_HOST": "0.0.0.0:11434"}),
            ("Docling Serve", "docling-serve", 5001,
             ["docling-serve", "run", "--host", "0.0.0.0", "--port", "5001"],
             "http://localhost:5001/ready",
             {"DOCLING_DEVICE": "mps", "DOCLING_SERVE_ENG_LOC_NUM_WORKERS": "1",
              "DOCLING_SERVE_SYNC_POLL_INTERVAL": "1"}),
        )
        for name, executable, port, _, _, _ in services:
            if shutil.which(executable) is None:
                raise RuntimeError(f"MPS requires `{executable}` on PATH; install the MPS dependencies first")
            try:
                with socket.create_connection(("localhost", port), timeout=1):
                    raise RuntimeError(
                        f"Port {port} is already in use; stop the existing {name} process before a clean MPS run"
                    )
            except OSError:
                pass

        self.native_logs_dir.mkdir(parents=True, exist_ok=True)
        for name, _, _, command, url, environment in services:
            log_path = self.native_logs_dir / f"{name.lower().replace(' ', '-')}.log"
            with log_path.open("wb") as output:
                process = subprocess.Popen(
                    command, env={**os.environ, **environment}, stdout=output,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            self.native_processes[name] = process
            print(f"Starting {name}", flush=True)
            self.wait_for_native_service(name=name, url=url, process=process, log_path=log_path)

    @staticmethod
    def wait_for_native_service(
        *, name: str, url: str, process: subprocess.Popen[bytes], log_path: Path,
    ) -> None:
        deadline = time.monotonic() + MPS_SERVICE_STARTUP_TIMEOUT_SECONDS
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"{name} exited with status {process.returncode}; see {log_path}"
                )
            try:
                with urlopen(url, timeout=5) as response:
                    response.read()
                print(f"{name} is ready", flush=True)
                return
            except OSError as error:
                last_error = error
            time.sleep(MPS_SERVICE_POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"{name} did not become ready at {url}; see {log_path}") from last_error

    def compose_command(self) -> list[str]:
        command = ["docker", "compose"]
        if self.args.env_file.is_file():
            command += ["--env-file", str(self.args.env_file)]
        return command + [
            "--project-name", self.args.project_name,
            "-f", str(self.args.compose_file), "-f", str(self.args.infra_file),
        ]

    def start_resource_collection(self):
        host = os.environ.get("DOCKER_HOST")
        if not host or os.environ.get("DOCKER_CONTEXT"):
            context = json.loads(subprocess.check_output(["docker", "context", "inspect"], text=True))[0]
            host = context["Endpoints"]["docker"]["Host"]
            self.client = docker.DockerClient(base_url=host, timeout=10)
        else:
            self.client = docker.from_env(timeout=10)
        profiles = self.args.colima_profile or []
        if self.args.skip_colima:
            profiles = []
        elif not profiles and ".colima" in Path(host).parts:
            parts = Path(host).parts
            profiles = [parts[parts.index(".colima") + 1]]
        self.args.colima_profile = profiles
        self.metadata["colima_profiles"] = profiles
        self.sampler = Sampler(args=self.args, client=self.client, run_dir=self.run_dir)
        self.sampler.start()

    def reset_containers(self):
        self.stack_started = True
        # Clear leftovers from an interrupted run before creating this environment.
        subprocess.run(self.compose_command() + ["down", "-v", "--remove-orphans"], check=True)

    def start_containers(self):
        subprocess.run(self.compose_command() + ["up", "-d", "--wait", "--no-deps", *self.args.infra_services], check=True)
        self.start_native_services()
        subprocess.run(self.compose_command() + ["up", "-d", "--wait", "--no-deps", "docpipe"], check=True)
        self.sampler.check()

    def download_embedding_models(self):
        self.sampler.phase = "model_download"
        self.metadata["embedding_models"] = self.embedding_models
        for model in self.embedding_models:
            if "ollama" in self.args.infra_services:
                subprocess.run(self.compose_command() + ["exec", "-T", "ollama", "ollama", "pull", model], check=True)
            else:
                payload = json.dumps({"name": model, "stream": False}).encode()
                with urlopen(Request(
                    "http://localhost:11434/api/pull", data=payload,
                    headers={"Content-Type": "application/json"},
                ), timeout=self.args.timeout_seconds) as response:
                    result = json.load(response)
                if result.get("error"):
                    raise RuntimeError(f"Ollama model pull failed: {result['error']}")
        self.sampler.check()

    def create_flow(self):
        flow_id = json.loads(request(args=self.args, path="/api/v1/flows", payload=self.flow))["flow_id"]
        self.metadata["flow_id"] = flow_id

    def submit_job(self):
        self.sampler.phase = "during"
        job = {"entity": {"job": {"asset_ref": self.metadata["flow_id"]}}}
        self.job_run_id = json.loads(request(args=self.args, path="/api/v1/job_runs", payload=job))["job_run_id"]
        self.metadata["job_run_id"] = self.sampler.job_run_id = self.job_run_id

    def wait_for_job_completion(self):
        deadline = time.monotonic() + self.args.timeout_seconds
        while True:
            self.sampler.check()
            self.payload = json.loads(request(args=self.args, path=f"/api/v1/job_runs/{self.job_run_id}"))
            status = self.payload["job_stats"]["status"]
            if status in TERMINAL_STATUSES:
                self.metadata["job_status"] = status
                if status not in {"Completed", "CompletedWithWarnings"}:
                    raise RuntimeError(f"Job run {self.job_run_id}: {status}")
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Job run {self.job_run_id} exceeded {self.args.timeout_seconds}s")
            self.sampler.wait(self.args.interval)

    def collect_job_run_files(self):
        if self.sampler:
            self.sampler.phase = "collection"
        if not self.job_run_id:
            return
        self.finish_step(action=self._collect_job_statistics)
        self.finish_step(action=self._copy_job_run_files)

    def _collect_job_statistics(self):
        self.payload = json.loads(request(
            args=self.args, path=f"/api/v1/job_runs/{self.job_run_id}?include_logs=true",
        ))
        (self.raw_dir / "job_stats.json").write_text(json.dumps(self.payload, indent=2) + "\n")
        logs = [self.payload[node] for node in self.payload.get("node_sequence", [])
                if isinstance(self.payload.get(node), str)]
        (self.raw_dir / "node_execution.log").write_text("\n\n".join(logs) + "\n")

    def _copy_job_run_files(self):
        source = subprocess.check_output(self.compose_command() + [
            "exec", "-T", "docpipe", "python", "-c",
            "import os, sys; from pathlib import Path; "
            "base = Path(os.environ.get('DOCPIPE_JOB_STATS_BASE_DIR') or "
            "str(Path(os.environ.get('DOCPIPE_DATA_PATH', './data')) / 'job_stats')); "
            "source = base / sys.argv[1]; print(source) if source.is_dir() else None",
            self.job_run_id,
        ], text=True).strip()
        self.metadata["job_run_files_available"] = bool(source)
        if not source:
            return
        subprocess.run(self.compose_command() + [
            "cp", f"docpipe:{source}", str(self.raw_dir / "job_run"),
        ], check=True)

    def collect_job_report(self):
        if self.job_run_id:
            (self.raw_dir / "summary.csv").write_bytes(request(
                args=self.args, path=f"/api/v1/job_runs/{self.job_run_id}/report",
            ))

    def collect_container_logs(self):
        if not self.stack_started:
            return
        identifiers = subprocess.check_output(
            self.compose_command() + ["ps", "--all", "-q", "docpipe"], text=True,
        ).split()
        if not identifiers:
            raise RuntimeError("No docpipe container available for log collection")
        with (self.raw_dir / "flow_run.log").open("wb") as output:
            for identifier in identifiers:
                subprocess.run(["docker", "logs", identifier], stdout=output, stderr=subprocess.STDOUT, check=True)

    def stop_containers(self):
        if self.sampler:
            self.sampler.phase = "teardown"
        if self.stack_started:
            # Both application and infrastructure files are included in this command.
            subprocess.run(self.compose_command() + ["down", "-v", "--remove-orphans"], check=True)

    def stop_native_services(self):
        if self.sampler:
            self.sampler.phase = "teardown"
        errors = []
        for name, process in reversed(list(self.native_processes.items())):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append(f"{name}: {error}")
                try:
                    process.terminate()
                except OSError as terminate_error:
                    errors.append(f"{name}: {terminate_error}")
            try:
                process.wait(timeout=MPS_SERVICE_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(f"{name}: {error}")
                    try:
                        process.kill()
                    except OSError as kill_error:
                        errors.append(f"{name}: {kill_error}")
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as error:
                    errors.append(f"{name}: {error}")
            except OSError as error:
                errors.append(f"{name}: {error}")
        self.native_processes.clear()
        if errors:
            raise RuntimeError("Failed to stop native MPS services: " + "; ".join(errors))

    def collect_native_service_logs(self):
        if self.args.case != "mps":
            return
        for name in ("docling-serve.log", "ollama.log"):
            source = self.native_logs_dir / name
            destination = self.raw_dir / name
            if source.is_file() and source.resolve() != destination.resolve():
                shutil.copyfile(source, destination)

    def stop_resource_collection(self):
        try:
            if self.sampler:
                self.sampler.close()
        finally:
            if self.client:
                self.client.close()

    def create_reports(self):
        if (self.raw_dir / "resource_samples.csv").exists():
            self.finish_step(action=lambda: summarize(run_dir=self.run_dir, job_run_id=self.job_run_id))
            self.finish_step(action=lambda: plot_samples(self.run_dir))
        self.metadata["collection_errors"] = " | ".join(self.errors)
        (self.raw_dir / "run_metadata.json").write_text(json.dumps(self.metadata, indent=2) + "\n")
        if not (self.raw_dir / "job_stats.json").exists() and self.payload:
            (self.raw_dir / "job_stats.json").write_text(json.dumps(self.payload, indent=2) + "\n")
        if (self.raw_dir / "job_stats.json").exists():
            self.finish_step(action=lambda: write_report(run_dir=self.run_dir))
            stats = json.loads((self.raw_dir / "job_stats.json").read_text())["job_stats"]
            row = dict.fromkeys(FIELDS, "")
            row.update(
                collection_id=self.run_dir.name, case=self.args.case, input_type=self.input_type,
                flow_name=self.flow_name, job_id=stats["job_id"], job_run_id=self.job_run_id,
                status=stats["status"], duration_seconds=stats.get("duration", 0),
                artifacts_dir=self.run_dir.name, collection_errors=" | ".join(self.errors),
                node_timings_seconds=json.dumps({node["name"]: node.get("time_taken", 0)
                                                for node in stats.get("node_stats", {}).values()}),
            )
            row.update({key: stats.get(key, 0) for key in ("completed_docs", "total_docs", "failed_docs", "skipped_docs")})
            (self.args.results_dir / "_").mkdir(exist_ok=True)
            append_results(destination=self.args.results_dir / "_" / "results.csv", row=row)
        else:
            (self.run_dir / "REPORT.md").write_text(
                f"# {self.flow_name} / {self.args.case}\n\nRun failed before job statistics were collected.\n\n"
                + "\n".join(f"- {error}" for error in self.errors) + "\n\nSupporting data: [_](_/).\n",
            )
        print(f"Report: {self.run_dir / 'REPORT.md'}", flush=True)

    def record_error(self, *, step: str, error: BaseException):
        message = f"{step}: {type(error).__name__}: {error}"
        self.errors.append(message)
        print(message, file=sys.stderr, flush=True)

    def finish_step(self, *, action):
        """Continue cleanup and reporting even when another collection step fails."""
        try:
            action()
        except Exception as error:
            self.record_error(step=action.__name__, error=error)


def main() -> int:
    runner = Runner(parse_arguments())
    try:
        runner.reset_containers()
        runner.start_resource_collection()
        runner.start_containers()
        runner.download_embedding_models()
        runner.create_flow()
        runner.submit_job()
        runner.wait_for_job_completion()
    except (Exception, KeyboardInterrupt) as error:
        runner.record_error(step="run", error=error)
    finally:
        runner.finish_step(action=runner.collect_job_run_files)
        runner.finish_step(action=runner.collect_job_report)
        runner.finish_step(action=runner.collect_container_logs)
        runner.finish_step(action=runner.stop_containers)
        runner.finish_step(action=runner.stop_native_services)
        runner.finish_step(action=runner.collect_native_service_logs)
        runner.finish_step(action=runner.stop_resource_collection)
        runner.finish_step(action=runner.create_reports)
    return 1 if runner.errors else 0


if __name__ == "__main__":
    sys.exit(main())
