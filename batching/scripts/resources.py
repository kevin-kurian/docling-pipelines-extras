"""Collect host-normalized service utilization and absolute CPU/memory usage."""

import json
import math
import platform
import re
import statistics
import subprocess
import threading
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

import docker
import psutil

from .models import ResourceSample, ResourceSummary


class ResourceCollector:
    def __init__(self) -> None:
        self._docker_client = self._connect_docker()
        try:
            docker_info = self._docker_client.info()
            self._host_cpu_count = psutil.cpu_count() or 1
            self._host_memory_total = psutil.virtual_memory().total
            self.capacity = {
                "host_cpu_count": self._host_cpu_count,
                "host_memory_bytes": self._host_memory_total,
                "colima_cpu_count": docker_info["NCPU"],
                "colima_memory_bytes": docker_info["MemTotal"],
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python_version": platform.python_version(),
                "gpu_metric_source": "ioreg: PerformanceStatistics/Device Utilization %",
                "gpu_aggregation": "maximum utilization across reported GPUs",
                "percentage_denominator": "host capacity for every CPU and memory scope",
                "memory_accounting": "native summed RSS; containers cache-adjusted working set; host used memory",
            }
            self._previous_cpu: dict[str, tuple[float, float]] = {}
            self._container_targets: dict[str, str] = {}
        except BaseException:
            self._docker_client.close()
            raise

    @staticmethod
    def _connect_docker() -> docker.DockerClient:
        socket_path = Path.home() / ".colima" / "default" / "docker.sock"
        return docker.DockerClient(base_url=f"unix://{socket_path}", timeout=5)

    def _make_sample(
        self,
        *,
        scope: str,
        target: str,
        identity: str,
        cpu_time_seconds: float,
        memory_bytes: int,
    ) -> ResourceSample:
        now = time.monotonic()
        previous = self._previous_cpu.get(identity)
        elapsed = now - previous[1] if previous else 0.0
        cpu_delta = max(0.0, cpu_time_seconds - previous[0]) if previous else 0.0
        cores = cpu_delta / elapsed if elapsed > 0 else None
        self._previous_cpu[identity] = cpu_time_seconds, now
        return ResourceSample(
            timestamp=time.time(),
            scope=scope,
            target=target,
            cpu_percent=cores / self._host_cpu_count * 100 if cores is not None else None,
            memory_percent=memory_bytes / self._host_memory_total * 100,
            cpu_cores=cores,
            memory_bytes=memory_bytes,
        )

    def close(self) -> None:
        self._docker_client.close()

    def _sample_host(self) -> ResourceSample:
        cpu = psutil.cpu_times()
        memory = psutil.virtual_memory()
        return self._make_sample(
            scope="host",
            target="mac",
            identity="host",
            cpu_time_seconds=cpu.user + cpu.system + cpu.nice,
            memory_bytes=self._host_memory_total - memory.available,
        )

    def _sample_container(self, *, container_name: str) -> ResourceSample:
        stats = self._docker_client.api.stats(container_name, stream=False, one_shot=True)
        memory = stats["memory_stats"]
        memory_stats = memory.get("stats", {})
        cache = memory_stats.get("inactive_file", memory_stats.get("total_inactive_file", 0))
        return self._make_sample(
            scope="container",
            target=self._container_targets[container_name],
            identity=f"container:{stats['id']}",
            cpu_time_seconds=stats["cpu_stats"]["cpu_usage"]["total_usage"] / 1_000_000_000,
            memory_bytes=max(0, memory["usage"] - cache),
        )

    def _sample_process(self, *, process_name: str, pid: int) -> ResourceSample:
        """Sum a native service's process tree after calculating per-PID interval counters."""
        root = psutil.Process(pid)
        samples = []
        for process in [root, *root.children(recursive=True)]:
            try:
                with process.oneshot():
                    cpu = process.cpu_times()
                    samples.append(
                        self._make_sample(
                            scope="process",
                            target=process_name,
                            identity=f"process:{process.pid}:{process.create_time()}",
                            cpu_time_seconds=cpu.user + cpu.system,
                            memory_bytes=process.memory_info().rss,
                        )
                    )
            except psutil.NoSuchProcess:
                # Children can finish between enumeration and reading their counters.
                if process.pid == pid:
                    raise
        return ResourceSample(
            timestamp=time.time(),
            scope="process",
            target=process_name,
            cpu_percent=sum(s.cpu_percent for s in samples if s.cpu_percent is not None)
            if any(s.cpu_percent is not None for s in samples)
            else None,
            cpu_cores=sum(s.cpu_cores for s in samples if s.cpu_cores is not None)
            if any(s.cpu_cores is not None for s in samples)
            else None,
            memory_percent=sum(s.memory_percent for s in samples),
            memory_bytes=sum(s.memory_bytes for s in samples),
        )

    def collect_cpu_and_memory(
        self,
        *,
        output_dir: Path,
        stop_event: threading.Event,
        ready_event: threading.Event,
        container_names: Sequence[str],
        processes: Mapping[str, int],
        interval_seconds: float = 1.0,
    ) -> Path:
        output_file = output_dir / "_resources" / "resource_samples.jsonl"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        (output_file.parent / "resource_capacity.json").write_text(
            json.dumps(self.capacity, indent=2) + "\n", encoding="utf-8"
        )
        for name in container_names:
            container = self._docker_client.containers.get(name)
            self._container_targets[name] = container.labels.get("com.docker.compose.service", name)
        with output_file.open("w", encoding="utf-8") as output:
            while not stop_event.is_set():
                started_at = time.monotonic()
                samples = [self._sample_host()]
                samples.extend(self._sample_container(container_name=name) for name in container_names)
                samples.extend(self._sample_process(process_name=name, pid=pid) for name, pid in processes.items())
                for sample in samples:
                    output.write(json.dumps(asdict(sample)) + "\n")
                output.flush()
                if all(sample.cpu_percent is not None for sample in samples):
                    ready_event.set()
                stop_event.wait(max(0, interval_seconds - (time.monotonic() - started_at)))
        return output_file

    @staticmethod
    def _read_gpu_utilization() -> float:
        """Read driver-reported GPU utilization without elevated privileges."""
        output = subprocess.check_output(
            ["/usr/sbin/ioreg", "-r", "-c", "IOAccelerator", "-d", "1", "-l", "-w", "0"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
        values = [float(value) for value in re.findall(r'"Device Utilization %"\s*=\s*(-?\d+(?:\.\d+)?)', output)]
        if not values:
            raise RuntimeError("GPU utilization is unavailable: IORegistry has no Device Utilization % counter")
        if any(not 0 <= value <= 100 for value in values):
            raise ValueError("IORegistry returned GPU utilization outside 0-100 percent")
        return max(values)

    @staticmethod
    def collect_gpu(
        *,
        output_dir: Path,
        stop_event: threading.Event,
        ready_event: threading.Event,
        interval_seconds: float = 1.0,
    ) -> Path:
        output_file = output_dir / "_resources" / "gpu_samples.jsonl"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("w", encoding="utf-8") as output:
            while not stop_event.is_set():
                started_at = time.monotonic()
                utilization = ResourceCollector._read_gpu_utilization()
                output.write(json.dumps({"timestamp": time.time(), "gpu_utilization_percent": utilization}) + "\n")
                output.flush()
                ready_event.set()
                stop_event.wait(max(0, interval_seconds - (time.monotonic() - started_at)))
        return output_file

    @staticmethod
    def _summarize(*, scope: str, target: str, metric: str, values: list[float]) -> ResourceSummary:
        ordered = sorted(values)
        return ResourceSummary(
            scope=scope,
            target=target,
            metric=metric,
            unit="bytes" if metric == "memory_bytes" else "cores" if metric == "cpu_cores" else "percent",
            samples=len(ordered),
            min_value=ordered[0],
            avg_value=statistics.fmean(ordered),
            p95_value=ordered[math.ceil(0.95 * len(ordered)) - 1],
            p99_value=ordered[math.ceil(0.99 * len(ordered)) - 1],
            max_value=ordered[-1],
        )

    @staticmethod
    def _aggregate_cpu_and_memory(
        *,
        output_dir: Path,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> list[ResourceSummary]:
        groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        with (output_dir / "_resources" / "resource_samples.jsonl").open(encoding="utf-8") as source:
            for line in source:
                sample = ResourceSample(**json.loads(line))
                if (start_time is not None and sample.timestamp < start_time) or (
                    end_time is not None and sample.timestamp > end_time
                ):
                    continue
                for metric in ("cpu_percent", "memory_percent", "cpu_cores", "memory_bytes"):
                    value = getattr(sample, metric)
                    if value is not None:
                        groups[sample.scope, sample.target, metric].append(value)
        if not groups:
            raise ValueError("No CPU/memory samples were collected")
        return [
            ResourceCollector._summarize(scope=scope, target=target, metric=metric, values=values)
            for (scope, target, metric), values in sorted(groups.items())
        ]

    @staticmethod
    def _aggregate_gpu(
        *,
        output_dir: Path,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> ResourceSummary:
        values = []
        with (output_dir / "_resources" / "gpu_samples.jsonl").open(encoding="utf-8") as source:
            for line in source:
                sample = json.loads(line)
                timestamp = sample["timestamp"]
                if (start_time is not None and timestamp < start_time) or (
                    end_time is not None and timestamp > end_time
                ):
                    continue
                values.append(sample["gpu_utilization_percent"])
        if not values:
            raise ValueError("No GPU utilization samples were collected")
        return ResourceCollector._summarize(scope="gpu", target="mac", metric="gpu_utilization_percent", values=values)

    @staticmethod
    def aggregate(
        *,
        output_dir: Path,
        errors: list[str],
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> list[ResourceSummary]:
        rows = []
        for label, aggregate in (
            ("CPU/memory", ResourceCollector._aggregate_cpu_and_memory),
            ("GPU", ResourceCollector._aggregate_gpu),
        ):
            try:
                result = aggregate(output_dir=output_dir, start_time=start_time, end_time=end_time)
                rows.extend(result if isinstance(result, list) else [result])
            except Exception as error:
                errors.append(f"{label} summary: {type(error).__name__}: {error}")
        return rows
