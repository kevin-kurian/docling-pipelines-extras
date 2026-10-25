import os
from collections.abc import Callable
from datetime import datetime
from threading import Event

from confluent_kafka import Consumer, KafkaException
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.json_schema import JSONDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

from .models import Notification


def consume_notifications(*, on_notification: Callable[[Notification], None], stop_event: Event) -> None:
    def notification_from_dict(data: dict, _context: SerializationContext) -> Notification:
        return Notification(
            event_id=data["event_id"], operation=data["operation"], status=data["status"],
            timestamp=datetime.fromisoformat(data["timestamp"]), pages_processed=data.get("pages_processed")
        )

    topic = os.environ["KAFKA_NOTIFICATION_TOPIC"]
    group_id = os.environ["KAFKA_NOTIFICATION_GROUP_ID"]
    registry = SchemaRegistryClient({"url": os.environ["SCHEMA_REGISTRY_URL"]})
    registered_schema = registry.get_latest_version(f"{topic}-value")
    deserializer = JSONDeserializer(
        registered_schema.schema.schema_str, from_dict=notification_from_dict, schema_registry_client=registry
    )
    context = SerializationContext(topic, MessageField.VALUE)
    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"],
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
    )
    try:
        consumer.subscribe([topic])
        while not stop_event.is_set():
            message = consumer.poll(timeout=1.0)
            if message is None:
                continue
            error = message.error()
            if error is not None:
                raise KafkaException(error)

            notification = deserializer(message.value(), context)
            on_notification(notification)
            for partition in consumer.commit(message=message, asynchronous=False):
                if partition.error is not None:
                    raise KafkaException(partition.error)
    finally:
        consumer.close()
