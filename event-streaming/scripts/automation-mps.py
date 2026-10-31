import json
import os
import signal
import subprocess
import time
from contextlib import ExitStack, suppress
from importlib import import_module
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

cpu = import_module(".automation-cpu", package=__package__)

PROJECT_NAME = "docpipe-event-streaming-mps"
ROUTING_KEY = "mps"
SERVICE_TIMEOUT_SECONDS = cpu.SERVICE_TIMEOUT_SECONDS


class StreamingAutomation(cpu.StreamingAutomation):
    def __init__(self) -> None:
        super().__init__()
        self.project_name = PROJECT_NAME
        self.routing_key = ROUTING_KEY
        self.compose_file = self.streaming_dir / "docker-compose-mps.yml"
        self.flow_file = self.streaming_dir / "flows" / "sample_flow_mps.json"
        self.processes: dict[str, subprocess.Popen] = {}

    def _start_processes(self, *, name: str, command: list[str], environment: dict[str, str], ready_url: str) -> None:
        log_dir = self.results_dir / "_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{name}.log"
        print(f"Starting native {name}; log: {log_file}", flush=True)
        with log_file.open("wb") as output:
            process = subprocess.Popen(
                command, env={**os.environ, **environment}, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes[name] = process
        deadline = time.monotonic() + SERVICE_TIMEOUT_SECONDS
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited during startup; see {log_file}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{name} readiness exceeded {SERVICE_TIMEOUT_SECONDS} seconds; see {log_file}")
            try:
                with urlopen(ready_url, timeout=5):
                    pass
                break
            except HTTPError as error:
                if error.code != 503:
                    raise
            except URLError as error:
                if not isinstance(error.reason, ConnectionRefusedError):
                    raise
            time.sleep(1)
        print(f"Native {name} is ready (PID {process.pid})", flush=True)

    def _stop_processes(self) -> None:
        with ExitStack() as cleanup:
            for name, process in self.processes.items():
                cleanup.callback(self._stop_process, name=name, process=process)

    def _stop_process(self, *, name: str, process: subprocess.Popen) -> None:
        print(f"Stopping native {name} (PID {process.pid})", flush=True)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        del self.processes[name]

    def _start_environment(self) -> None:
        wait = ["up", "-d", "--wait", "--wait-timeout", str(SERVICE_TIMEOUT_SECONDS)]
        compose_files = [self.kafka_compose_file, self.compose_file]

        self._print_section("CONTAINER CREATION")
        print("Creating Kafka event-streaming containers", flush=True)
        self._run_compose(compose_files=[self.kafka_compose_file], arguments=[*wait, "kafka", "schema-registry"])
        print("Initializing Kafka topics and registering schemas", flush=True)
        self._run_compose(compose_files=[self.kafka_compose_file], arguments=["run", "--rm", "-T", "register-schema"])
        print("Creating OpenSearch container", flush=True)
        self._run_compose(compose_files=compose_files, arguments=[*wait, "opensearch"])

        self._print_section("NATIVE PROCESS CREATION")
        self._start_processes(
            name="ollama", command=["ollama", "serve"], environment={"OLLAMA_HOST": "0.0.0.0:11435"},
            ready_url="http://localhost:11435/api/tags",
        )
        self._start_processes(
            name="docling-serve", command=["docling-serve", "run", "--host", "0.0.0.0", "--port", "5001"],
            environment={
                "DOCLING_DEVICE": "mps", "DOCLING_SERVE_ENG_LOC_NUM_WORKERS": "1",
                "DOCLING_SERVE_SYNC_POLL_INTERVAL": "1",
            },
            ready_url="http://localhost:5001/ready",
        )

        print("Creating MPS Docpipe container", flush=True)
        self._run_compose(compose_files=compose_files, arguments=[*wait, "--no-deps", "docpipe"])
        print("MPS event-streaming services are healthy", flush=True)

        print("Preparing Ollama embedding model: nomic-embed-text", flush=True)
        request = Request(
            "http://localhost:11435/api/pull",
            data=json.dumps({"model": "nomic-embed-text", "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=SERVICE_TIMEOUT_SECONDS) as response:
            response.read()
        print("Ollama embedding model is ready", flush=True)

    def _stop_environment(self) -> None:
        self._print_section("CONTAINER TEARDOWN")
        print("Removing MPS event-streaming containers", flush=True)
        try:
            self._run_compose(
                compose_files=[self.kafka_compose_file, self.compose_file], arguments=["down", "--volumes"],
            )
        finally:
            self._print_section("NATIVE PROCESS TEARDOWN")
            self._stop_processes()
