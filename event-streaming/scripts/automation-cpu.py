import json
import os
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from threading import Condition, Event, Thread
from urllib.request import Request, urlopen

from .kafka_consume import consume_notifications
from .kafka_produce import produce_events
from .models import EventTiming, Notification, OperationTiming
from .reports import TimingReports

PROJECT_NAME = "docpipe-event-streaming-cpu"
ROUTING_KEY = "cpu"
SERVICE_TIMEOUT_SECONDS = 600
COMPLETION_TIMEOUT_SECONDS = 1800
STREAMING_DIR = Path(__file__).resolve().parents[1]
DATASETS_DIR = STREAMING_DIR.parent / "datasets"
DATASET_PATHS = {
    "ocr": (Path("10-pdf-files/pdf/ocr/warmup"), Path("10-pdf-files/pdf/ocr")),
    "non-ocr": (Path("10-pdf-files/pdf/non-ocr/warmup"), Path("10-pdf-files/pdf/non-ocr")),
    "text": (Path("10-text-files/text/warmup"), Path("10-text-files/text")),
}


def get_dataset_folders(*, file_type: str) -> tuple[Path, Path]:
    warmup, measured = (DATASETS_DIR / path for path in DATASET_PATHS[file_type])
    for directory in (warmup, measured):
        if not any(path.is_file() for path in directory.iterdir()):
            raise ValueError(f"Dataset folder contains no files: {directory}")
    return warmup, measured


def create_results_directory(*, root: Path, name: str) -> Path:
    directory = root / f"{datetime.now().astimezone():%Y%m%d_%H%M%S}_{name}"
    directory.mkdir(parents=True)
    return directory


class StreamingAutomation:
    def __init__(self) -> None:
        self.project_name = PROJECT_NAME
        self.routing_key = ROUTING_KEY
        self.streaming_dir = STREAMING_DIR
        self.compose_file = self.streaming_dir / "docker-compose-cpu.yml"
        self.kafka_compose_file = self.streaming_dir / "docker-compose-kafka.yml"
        self.flow_file = self.streaming_dir / "flows" / "sample_flow_cpu.json"
        self.results_dir: Path
        self.reports: TimingReports

        self.consumer_thread: Thread
        self.consumer_stop_event = Event()
        self.event_timings: dict[str, EventTiming] = {}
        self.completion_condition = Condition()
        self.completed_event_ids: set[str] = set()

        self.consumer_error: Exception | None = None

    @staticmethod
    def _print_section(title: str) -> None:
        print(f"\n--- {title} ---\n", flush=True)

    def _run_compose(
            self, *, compose_files: Sequence[Path], arguments: Sequence[str],
            timeout_seconds: float = SERVICE_TIMEOUT_SECONDS + 60
    ) -> None:
        command = ["docker", "--context", "colima", "compose", "--project-name", self.project_name]
        for compose_file in compose_files:
            command.extend(["-f", str(compose_file)])
        command.extend(arguments)
        subprocess.run(command, cwd=self.streaming_dir, timeout=timeout_seconds, check=True)

    def _create_flow(self, *, file_type: str) -> dict:
        base_url = os.environ["DOCPIPE_API_BASE_URL"].rstrip("/")
        payload = json.loads(self.flow_file.read_text())
        for operator in payload["flow"]:
            if operator["type"] == "ingest_source":
                operator["config"]["include_filter"] = "txt" if file_type == "text" else "pdf"
            elif operator["type"] == "extract_operator":
                operator["config"]["text_extraction"]["provider_config"]["ocr"]["enabled"] = file_type == "ocr"
        request = Request(
            f"{base_url}/api/v1/flows", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def _create_routing_key(self, *, routing_key: str, flow_id: str) -> dict:
        base_url = os.environ["DOCPIPE_API_BASE_URL"].rstrip("/")
        payload = json.dumps({"key": routing_key, "flow_id": flow_id}).encode("utf-8")
        request = Request(
            f"{base_url}/api/v1/event_mappings", data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def _start_environment(self) -> None:
        wait = ["up", "-d", "--wait", "--wait-timeout", str(SERVICE_TIMEOUT_SECONDS)]

        self._print_section("CONTAINER CREATION")
        print("Creating CPU event-streaming containers", flush=True)
        self._run_compose(compose_files=[self.kafka_compose_file, self.compose_file], arguments=wait)
        print("CPU event-streaming services are healthy", flush=True)

        print("Preparing Ollama embedding model: nomic-embed-text", flush=True)
        self._run_compose(
            compose_files=[self.kafka_compose_file, self.compose_file],
            arguments=["exec", "-T", "ollama", "ollama", "pull", "nomic-embed-text"]
        )
        print("Ollama embedding model is ready", flush=True)

    def _stop_environment(self) -> None:
        down = ["down", "--volumes"]

        self._print_section("CONTAINER TEARDOWN")
        print("Removing CPU event-streaming containers", flush=True)
        self._run_compose(compose_files=[self.kafka_compose_file, self.compose_file], arguments=down)

    def _record_notification(self, *, notification: Notification) -> None:
        event_id = notification.event_id
        event_timing = self.event_timings.setdefault(event_id, EventTiming(event_id=event_id))

        if notification.status == "Received":
            event_timing.received_at = notification.timestamp
            return

        operation = notification.operation
        operation_timing = (
            event_timing.flow if operation == "flow"
            else event_timing.operators.setdefault(operation, OperationTiming())
        )

        if notification.status == "Started":
            operation_timing.started_at = notification.timestamp
            return

        operation_timing.ended_at = notification.timestamp
        if notification.pages_processed is not None:
            operation_timing.pages_processed = notification.pages_processed

    def _start_notification_consumer(self) -> None:
        def on_notification(notification: Notification) -> None:
            with self.completion_condition:
                self._record_notification(notification=notification)
                if notification.operation == "flow" and notification.status == "Completed":
                    self.completed_event_ids.add(notification.event_id)
                    self.completion_condition.notify_all()

        def consume() -> None:
            try:
                consume_notifications(on_notification=on_notification, stop_event=self.consumer_stop_event)
            except Exception as error:
                with self.completion_condition:
                    self.consumer_error = error
                    self.completion_condition.notify_all()

        self.consumer_stop_event.clear()
        with self.completion_condition:
            self.completed_event_ids.clear()
            self.event_timings.clear()
            self.consumer_error = None
        self.consumer_thread = Thread(target=consume, name=f"{self.project_name}-notifications", daemon=True)
        self.consumer_thread.start()

    def _stop_notification_consumer(self) -> None:
        self.consumer_stop_event.set()
        self.consumer_thread.join(timeout=30)
        if self.consumer_thread.is_alive():
            raise TimeoutError("Notification consumer did not stop within 30 seconds")
        if self.consumer_error is not None:
            raise self.consumer_error

    def _wait_for_completion(self, *, event_ids: Sequence[str], phase: str) -> None:
        expected_ids = set(event_ids)
        print(f"{phase}: waiting for {len(expected_ids)} events to complete", flush=True)
        with self.completion_condition:
            if not self.completion_condition.wait_for(
                    lambda: expected_ids <= self.completed_event_ids or self.consumer_error is not None,
                    timeout=COMPLETION_TIMEOUT_SECONDS,
            ):
                raise TimeoutError(f"{phase}: flow completion timed out after {COMPLETION_TIMEOUT_SECONDS}s")
            if self.consumer_error is not None:
                raise self.consumer_error

        print(f"{phase}: all {len(expected_ids)} events completed", flush=True)

    def _run_phase(self, *, host_folder: Path, docker_folder: Path, routing_key: str, phase: str) -> list[str]:
        self._print_section(phase.upper())
        self._start_notification_consumer()
        try:
            print(f"{phase}: publishing files from {host_folder}", flush=True)
            event_ids = produce_events(host_folder=host_folder, docker_folder=docker_folder, routing_key=routing_key)
            self._wait_for_completion(event_ids=event_ids, phase=phase)
        finally:
            self._stop_notification_consumer()
        self.reports.write(event_ids=event_ids, phase=phase)
        return event_ids

    def run(self, *, file_type: str, results_root: Path) -> list[str]:
        host_warmup_folder, host_folder = get_dataset_folders(file_type=file_type)
        warmup_path, measured_path = DATASET_PATHS[file_type]
        self.results_dir = create_results_directory(
            root=results_root, name=f"{self.flow_file.stem}_{file_type}",
        )
        self.reports = TimingReports(
            output_dir=self.results_dir, event_timings=self.event_timings,
            operations=[operator["name"] for operator in json.loads(self.flow_file.read_text())["flow"]],
        )
        try:
            self._start_environment()
            self._print_section("FLOW SETUP")
            flow = self._create_flow(file_type=file_type)
            mapping = self._create_routing_key(routing_key=self.routing_key, flow_id=flow["flow_id"])
            print(f"Flow ready with routing key {mapping['key']}", flush=True)

            self._run_phase(
                host_folder=host_warmup_folder, docker_folder=Path("/documents") / warmup_path,
                routing_key=mapping["key"], phase="Warm-up",
            )
            return self._run_phase(
                host_folder=host_folder, docker_folder=Path("/documents") / measured_path,
                routing_key=mapping["key"], phase="Processing run",
            )
        finally:
            self._stop_environment()
