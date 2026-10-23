"""Run CPU/MPS benchmarks and retain reports for every case, including failures."""

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import psutil

from .console import PROGRESS_INTERVAL_SECONDS, activity, log
from .models import BatchRunResult, Environment, FileType

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "results" / "batching"
PROJECT_NAME = "docpipe-batching-tests"
COLLECTOR_TIMEOUT_SECONDS = 60
JOB_TIMEOUT_SECONDS = 3600
SERVICE_TIMEOUT_SECONDS = 600
SUCCESS_STATUSES = {"Completed", "CompletedWithWarnings"}
TERMINAL_STATUSES = SUCCESS_STATUSES | {"CompletedWithErrors", "Failed", "Canceled", "Aborted"}


def _timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="microseconds")


def _error_text(error: BaseException) -> str:
    message = f"{type(error).__name__}: {error}"
    if isinstance(error, subprocess.CalledProcessError) and error.output:
        message += f"\n{error.output[-4000:]}"
    return message


def create_run_directory(*, flow_file: Path, output_dir: Path) -> Path:
    stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = output_dir / f"{stamp}_{flow_file.stem}"
    run_dir.mkdir(parents=True)
    return run_dir


class BatchAutomation:
    def __init__(
        self,
        *,
        collector_timeout_seconds: float = COLLECTOR_TIMEOUT_SECONDS,
        job_timeout_seconds: float = JOB_TIMEOUT_SECONDS,
    ) -> None:
        self.batching_dir = Path(__file__).resolve().parents[1]
        self.infra_compose_file = self.batching_dir / "docker-compose-infra.yml"
        self.cpu_compose_file = self.batching_dir / "docker-compose-cpu.yml"
        self.mps_compose_file = self.batching_dir / "docker-compose-mps.yml"
        self.base_url = os.environ["DOCPIPE_API_BASE_URL"].rstrip("/")
        self.timestamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d_%H-%M-%S")
        self.summary_dir = DEFAULT_OUTPUT_DIR / f"{self.timestamp}_suite"
        self.summary_dir.mkdir(parents=True)
        self.processes: dict[str, subprocess.Popen] = {}
        self._native_state_path = DEFAULT_OUTPUT_DIR / ".native_processes.json"
        self._log_dir = self.summary_dir / "_logs"
        self.collector_timeout_seconds = collector_timeout_seconds
        self.job_timeout_seconds = job_timeout_seconds

    @staticmethod
    def _request(*, url: str, payload: dict | None = None, timeout_seconds: float = 30) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.load(response)

    def _run_compose(
        self,
        *,
        compose_file: Path,
        arguments: Sequence[str],
        timeout_seconds: float = SERVICE_TIMEOUT_SECONDS + 60,
    ) -> str:
        command = [
            "docker",
            "--context",
            "colima",
            "compose",
            "--project-name",
            PROJECT_NAME,
            "--env-file",
            str(self.batching_dir / ".env"),
            "-f",
            str(compose_file),
            *arguments,
        ]
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=timeout_seconds)

    def _read_native_records(self) -> dict:
        if not self._native_state_path.exists():
            return {}
        records = json.loads(self._native_state_path.read_text(encoding="utf-8"))
        if not isinstance(records, dict):
            raise ValueError("Native process registry must be an object")
        return records

    def _write_native_records(self, records: dict) -> None:
        if records:
            temporary = self._native_state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self._native_state_path)
        else:
            self._native_state_path.unlink(missing_ok=True)

    def _start_processes(
        self,
        *,
        name: str,
        command: list[str],
        environment: dict[str, str],
        ready_url: str,
    ) -> None:
        log(f"Starting native {name}; log: _logs/{name}.log", label="START")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        with (self._log_dir / f"{name}.log").open("wb") as output:
            process = subprocess.Popen(
                command,
                env={**os.environ, **environment},
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes[name] = process
        records = self._read_native_records()
        records[name] = {"pid": process.pid, "created_at": psutil.Process(process.pid).create_time()}
        self._write_native_records(records)
        deadline = time.monotonic() + SERVICE_TIMEOUT_SECONDS
        with activity(f"Native {name} running (PID {process.pid}); waiting for readiness", label="PROCESS"):
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited during startup; see {self._log_dir / f'{name}.log'}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{name} readiness exceeded {SERVICE_TIMEOUT_SECONDS} seconds")
                try:
                    with urlopen(ready_url, timeout=min(5, remaining)):
                        pass
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} exited during readiness check")
                    break
                except OSError:
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
        log(f"Native {name} ready (PID {process.pid})", label="READY")

    def _start_environment(self, *, environment: Environment) -> None:
        wait = ["up", "-d", "--wait", "--wait-timeout", str(SERVICE_TIMEOUT_SECONDS)]
        if environment == "cpu":
            with activity("Starting containers: docling-serve, ollama, opensearch, docpipe", label="START"):
                self._run_compose(compose_file=self.cpu_compose_file, arguments=wait)
            log("Containers ready: docling-serve, ollama, opensearch, docpipe", label="READY")
        else:
            with activity("Starting container: opensearch", label="START"):
                self._run_compose(compose_file=self.infra_compose_file, arguments=[*wait, "opensearch"])
            log("Container ready: opensearch", label="READY")
            self._start_processes(
                name="ollama",
                command=["ollama", "serve"],
                environment={"OLLAMA_HOST": "0.0.0.0:11434"},
                ready_url="http://localhost:11434/api/tags",
            )
            self._start_processes(
                name="docling-serve",
                command=["docling-serve", "run", "--host", "0.0.0.0", "--port", "5001"],
                environment={
                    "DOCLING_DEVICE": "mps",
                    "DOCLING_SERVE_ENG_LOC_NUM_WORKERS": "1",
                    "DOCLING_SERVE_SYNC_POLL_INTERVAL": "1",
                },
                ready_url="http://localhost:5001/ready",
            )
            with activity("Starting container: docpipe", label="START"):
                self._run_compose(
                    compose_file=self.mps_compose_file,
                    arguments=[*wait, "--no-deps", "docpipe"],
                )
            log("Container ready: docpipe", label="READY")
        with activity("Preparing embedding model: nomic-embed-text (download if needed)", label="MODEL"):
            self._request(
                url="http://localhost:11434/api/pull",
                payload={"model": "nomic-embed-text", "stream": False},
                timeout_seconds=SERVICE_TIMEOUT_SECONDS,
            )
        log("Embedding model ready: nomic-embed-text", label="READY")

    def _stop_environment(self, *, environment: str) -> None:
        """Clean the benchmark project and owned native process groups, including previous invocations."""
        errors = []
        try:
            records = self._read_native_records()
        except Exception as error:
            records = {}
            errors.append(f"native process registry: {_error_text(error)}")
        for name, process in self.processes.items():
            if name not in records:
                try:
                    records[name] = {"pid": process.pid, "created_at": psutil.Process(process.pid).create_time()}
                except psutil.NoSuchProcess:
                    pass
                except Exception as error:
                    errors.append(f"{name}: {_error_text(error)}")
        remaining_records = {}
        for name, record in records.items():
            try:
                pid = record["pid"]
                process = psutil.Process(pid)
                if process.create_time() != record["created_at"]:
                    log(f"Skipping stale native {name} record (PID {pid} reused)", label="CLEANUP")
                    continue  # The recorded PID has since been reused by another process.
                if os.getpgid(pid) != pid:
                    raise RuntimeError(f"Refusing to signal an unowned process group for {name}")
                log(f"Stopping native {name} (PID {pid})", label="STOP")
                os.killpg(pid, signal.SIGTERM)
                waiter = self.processes.get(name, process)
                try:
                    waiter.wait(timeout=10)
                except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
                    log(f"Native {name} did not exit after 10s; forcing shutdown", label="WARN")
                    os.killpg(pid, signal.SIGKILL)
                    waiter.wait(timeout=5)
                log(f"Native {name} stopped", label="DONE")
            except (ProcessLookupError, psutil.NoSuchProcess):
                log(f"Native {name} already exited", label="CLEANUP")
            except Exception as error:
                remaining_records[name] = record
                errors.append(f"{name}: {_error_text(error)}")
        try:
            self._write_native_records(remaining_records)
        except Exception as error:
            errors.append(f"native process registry: {_error_text(error)}")
        self.processes = {name: process for name, process in self.processes.items() if name in remaining_records}
        compose_file = self.mps_compose_file if environment == "mps" else self.cpu_compose_file
        try:
            with activity("Stopping benchmark containers and removing run volumes", label="STOP"):
                self._run_compose(
                    compose_file=compose_file,
                    arguments=["down", "--volumes", "--remove-orphans", "--timeout", "15"],
                    timeout_seconds=60,
                )
            log("Benchmark containers stopped; run volumes removed", label="DONE")
        except Exception as error:
            errors.append(f"Docker cleanup: {_error_text(error)}")
        if errors:
            raise RuntimeError("; ".join(errors))

    @staticmethod
    def _check_collectors(futures: Sequence[Future]) -> None:
        for future in futures:
            if future.done():
                future.result()
                raise RuntimeError("Resource collector stopped before the job completed")

    def _wait_for_collectors(self, *, ready_events: Sequence[threading.Event], futures: Sequence[Future]) -> None:
        deadline = time.monotonic() + self.collector_timeout_seconds
        while not all(event.is_set() for event in ready_events):
            self._check_collectors(futures)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Collector readiness exceeded {self.collector_timeout_seconds:g} seconds")
            time.sleep(min(0.1, remaining))
        self._check_collectors(futures)

    def _wait_for_job(self, *, job_run_id: str, futures: Sequence[Future]) -> str:
        started = time.monotonic()
        deadline = started + self.job_timeout_seconds
        last_message = started
        previous_status = None
        while True:
            self._check_collectors(futures)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Job {job_run_id} exceeded {self.job_timeout_seconds:g} seconds")
            response = self._request(
                url=f"{self.base_url}/api/v1/job_runs/{job_run_id}",
                timeout_seconds=min(30, remaining),
            )
            stats = response["job_stats"]
            status = stats["status"]
            now = time.monotonic()
            if status != previous_status or now - last_message >= PROGRESS_INTERVAL_SECONDS:
                counts = ""
                if stats.get("completed_docs") is not None:
                    total = f"/{stats['total_docs']}" if stats.get("total_docs") is not None else ""
                    counts = f"; {stats['completed_docs']}{total} docs completed"
                log(f"{status} ({now - started:.0f}s elapsed{counts})", label="JOB")
                previous_status, last_message = status, now
            if status in SUCCESS_STATUSES:
                return status
            if status in TERMINAL_STATUSES:
                raise RuntimeError(f"Job {job_run_id} ended with {status}")
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def _get_container_names(self, *, environment: str) -> list[str]:
        services = ["opensearch"] if environment == "mps" else ["docling-serve", "ollama", "opensearch"]
        names = self._run_compose(
            compose_file=self.infra_compose_file,
            arguments=["ps", "--format", "{{.Name}}", *services],
        ).splitlines()
        names.extend(
            self._run_compose(
                compose_file=self.batching_dir / f"docker-compose-{environment}.yml",
                arguments=["ps", "--format", "{{.Name}}", "docpipe"],
            ).splitlines()
        )
        return names

    def _run_automation(self, *, flow_file: Path, run: BatchRunResult) -> None:
        from .resources import ResourceCollector

        flow = json.loads(flow_file.read_text(encoding="utf-8"))
        flow["flow_name"] = run.output_dir.name
        run.configuration = {"global_config": flow.get("global_config", {}), "flow_file": flow_file.name}
        for node in flow["flow"]:
            if node["type"] == "extract_operator":
                config = node["config"]
                run.configuration["extraction_max_workers"] = config.get("max_workers")
                run.configuration["ocr"] = config.get("text_extraction", {}).get("provider_config", {}).get("ocr")
        log(f"Registering flow: {flow_file.stem}", label="FLOW")
        flow_id = self._request(url=f"{self.base_url}/api/v1/flows", payload=flow)["flow_id"]
        collector = ResourceCollector()
        stop_event = threading.Event()
        try:
            names = self._get_container_names(environment=run.environment)
            processes = {name: process.pid for name, process in self.processes.items()}
            ready_events = [threading.Event(), threading.Event()]
            log("Starting CPU, memory and GPU sampling", label="RESOURCES")
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        collector.collect_cpu_and_memory,
                        output_dir=run.output_dir,
                        stop_event=stop_event,
                        ready_event=ready_events[0],
                        container_names=names,
                        processes=processes,
                    ),
                    pool.submit(
                        collector.collect_gpu,
                        output_dir=run.output_dir,
                        stop_event=stop_event,
                        ready_event=ready_events[1],
                    ),
                ]
                try:
                    with activity("Waiting for resource collectors", label="RESOURCES"):
                        self._wait_for_collectors(ready_events=ready_events, futures=futures)
                    log("CPU, memory and GPU collectors ready", label="READY")
                    log("Submitting job", label="JOB")
                    run.measurement_started_at = time.time()
                    run.job_run_id = self._request(
                        url=f"{self.base_url}/api/v1/job_runs",
                        payload={"entity": {"job": {"asset_ref": flow_id}}},
                    )["job_run_id"]
                    log(f"Submitted: {run.job_run_id}", label="JOB")
                    run.status = self._wait_for_job(job_run_id=run.job_run_id, futures=futures)
                finally:
                    run.measurement_ended_at = time.time()
                    stop_event.set()
                for future in futures:
                    future.result()
        finally:
            stop_event.set()
            try:
                collector.close()
            except Exception as error:
                run.diagnostic_errors.append(f"collector close: {_error_text(error)}")
            log("Resource sampling stopped", label="RESOURCES")

    def _collect_run_stats(self, *, run: BatchRunResult) -> None:
        from .resources import ResourceCollector
        from .stats import JobStatsCollector

        if run.job_run_id:
            collector = JobStatsCollector(job_run_id=run.job_run_id, output_dir=run.output_dir)
            try:
                run.job_stats = collector.build_job_run_stats(environment=run.environment, file_type=run.file_type)
            except Exception as error:
                run.diagnostic_errors.append(f"job statistics: {_error_text(error)}")
        if (run.output_dir / "_resources").exists():
            run.resources = ResourceCollector.aggregate(
                output_dir=run.output_dir,
                errors=run.diagnostic_errors,
                start_time=run.measurement_started_at,
                end_time=run.measurement_ended_at,
            )

    def _capture_logs(self, *, run: BatchRunResult) -> None:
        try:
            logs = self._run_compose(
                compose_file=self.batching_dir / f"docker-compose-{run.environment}.yml",
                arguments=["logs", "--no-color", "--timestamps", "--tail", "2000"],
                timeout_seconds=30,
            )
            log_dir = run.output_dir / "_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "services.log").write_text(logs, encoding="utf-8")
        except Exception as error:
            run.diagnostic_errors.append(f"service logs: {_error_text(error)}")

    def _write_summary(self, *, runs: Sequence[BatchRunResult]) -> None:
        from .reports import SuiteReports

        SuiteReports.write(output_dir=self.summary_dir, runs=runs)

    def run(self, *, environments: Sequence[str], file_types: Sequence[str]) -> list[BatchRunResult]:
        runs = []
        total_cases = len(environments) * len(file_types)
        log(f"{total_cases} benchmark case(s); results: {self.summary_dir}", label="SUITE")
        for environment in environments:
            for file_type in file_types:
                flow_file = self.batching_dir / "flows" / f"sample_flow_{environment}_{file_type}.json"
                run = BatchRunResult(
                    environment=cast(Environment, environment),
                    file_type=cast(FileType, file_type.replace("_", "-")),
                    output_dir=create_run_directory(flow_file=flow_file, output_dir=self.summary_dir),
                    started_at=_timestamp(),
                    status="Running",
                )
                runs.append(run)
                self._log_dir = run.output_dir / "_logs"
                started = time.monotonic()
                interrupted = False
                case_name = f"{environment.upper()} / {file_type.replace('_', ' ').upper()}"
                log(case_name, label=f"CASE {len(runs)}/{total_cases}", blank=True)
                try:
                    self._write_summary(runs=runs)
                    log("Removing leftovers from previous runs", label="CLEANUP")
                    self._stop_environment(environment=environment)
                    self._start_environment(environment=cast(Environment, environment))
                    self._run_automation(flow_file=flow_file, run=run)
                except KeyboardInterrupt:
                    run.status, run.error, interrupted = "Interrupted", "Interrupted by user", True
                    log("Interrupted; collecting diagnostics and stopping services", label="WARN")
                except Exception as error:
                    run.status = "TimedOut" if isinstance(error, TimeoutError) else "Failed"
                    run.error = _error_text(error)
                    log(run.error, label="ERROR")
                finally:
                    # Preserve diagnostics before teardown removes the service volumes.
                    with activity("Collecting job stats, resource summaries and service logs", label="COLLECT"):
                        try:
                            self._collect_run_stats(run=run)
                        except Exception as error:
                            run.diagnostic_errors.append(f"reports: {_error_text(error)}")
                        self._capture_logs(run=run)
                    try:
                        self._stop_environment(environment=environment)
                    except Exception as error:
                        run.cleanup_errors.append(_error_text(error))
                    if run.status in SUCCESS_STATUSES and (run.diagnostic_errors or run.cleanup_errors):
                        run.status = "Failed"
                        run.error = "Job completed but report collection or cleanup failed"
                        log(run.error, label="ERROR")
                    for error in run.diagnostic_errors:
                        log(error, label="WARN")
                    for error in run.cleanup_errors:
                        log(error, label="ERROR")
                    run.ended_at = _timestamp()
                    run.case_duration_seconds = time.monotonic() - started
                    with activity("Writing run and suite CSV/HTML reports", label="REPORT"):
                        self._write_summary(runs=runs)
                summary = []
                if run.job_stats:
                    if run.job_stats.completed_docs is not None:
                        summary.append(f"{run.job_stats.completed_docs} docs completed")
                    if run.job_stats.total_execution_time_seconds is not None:
                        summary.append(f"{run.job_stats.total_execution_time_seconds:g}s job")
                summary.append(f"{run.case_duration_seconds:.0f}s total")
                log(
                    f"{case_name}: {run.status} | {' | '.join(summary)}",
                    label="DONE" if run.status in SUCCESS_STATUSES else "ERROR",
                )
                if interrupted:
                    log(f"Partial report: {self.summary_dir / 'consolidated_report.html'}", label="REPORT")
                    raise KeyboardInterrupt
        passed = sum(run.status in SUCCESS_STATUSES for run in runs)
        log(f"Finished {len(runs)} case(s): {passed} succeeded, {len(runs) - passed} failed", label="SUITE", blank=True)
        log(str(self.summary_dir / "consolidated_report.html"), label="REPORT")
        return runs


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not 0 < seconds < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be a finite positive number of seconds")
    return seconds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", nargs="+", choices=("cpu", "mps"), default=["cpu", "mps"])
    parser.add_argument(
        "--file-type",
        nargs="+",
        choices=("pdf_ocr", "pdf_non_ocr", "text"),
        default=["pdf_ocr", "pdf_non_ocr", "text"],
    )
    parser.add_argument("--collector-timeout-seconds", type=_positive_seconds, default=COLLECTOR_TIMEOUT_SECONDS)
    parser.add_argument("--job-timeout-seconds", type=_positive_seconds, default=JOB_TIMEOUT_SECONDS)
    args = parser.parse_args()
    env_file = Path(__file__).resolve().parents[1] / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", maxsplit=1)
            os.environ.setdefault(name, value)
    try:
        runs = BatchAutomation(
            collector_timeout_seconds=args.collector_timeout_seconds,
            job_timeout_seconds=args.job_timeout_seconds,
        ).run(environments=list(dict.fromkeys(args.environment)), file_types=list(dict.fromkeys(args.file_type)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    if any(run.status not in SUCCESS_STATUSES for run in runs):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
