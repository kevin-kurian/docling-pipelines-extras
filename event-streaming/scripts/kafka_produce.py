import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from confluent_kafka import KafkaError, KafkaException, Message, Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.json_schema import JSONSerializer
from confluent_kafka.serialization import MessageField, SerializationContext


class KafkaProducer:
    def __init__(self) -> None:
        self._topic = os.environ["KAFKA_INPUT_TOPIC"]
        registry = SchemaRegistryClient({"url": os.environ["SCHEMA_REGISTRY_URL"]})
        registered_schema = registry.get_latest_version(f"{self._topic}-value")
        self._serializer = JSONSerializer(
            registered_schema.schema.schema_str, schema_registry_client=registry, conf={"auto.register.schemas": False}
        )
        self._context = SerializationContext(self._topic, MessageField.VALUE)
        self._producer = Producer({"bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"]})

    def produce(self, message: dict, *, on_delivery: Callable[[KafkaError | None, Message], None]) -> None:
        self._producer.produce(
            topic=self._topic, key=str(message["event_id"]),
            value=self._serializer(message, self._context), on_delivery=on_delivery
        )
        self._producer.poll(0)

    def flush(self) -> None:
        remaining = self._producer.flush(timeout=30.0)
        if remaining:
            raise TimeoutError(f"{remaining} file events were not delivered to {self._topic} within 30 seconds")


def produce_events(*, host_folder: Path, docker_folder: Path, routing_key: str) -> list[str]:
    files = sorted(path for path in host_folder.iterdir() if path.is_file())
    if not files:
        raise ValueError(f"Dataset folder contains no files: {host_folder}")

    producer = KafkaProducer()
    event_ids: list[str] = []

    def on_delivery(error: KafkaError | None, _message: Message) -> None:
        if error is not None:
            raise KafkaException(error)

    for file_path in files:
        event_id = str(uuid4())
        event = {
            "event_id": event_id,
            "event_timestamp": datetime.now(UTC).isoformat(),
            "routing_key": routing_key,
            "operation": "create",
            "file_reference": str(docker_folder / file_path.name),
        }
        producer.produce(event, on_delivery=on_delivery)
        event_ids.append(event_id)
    producer.flush()

    return event_ids
