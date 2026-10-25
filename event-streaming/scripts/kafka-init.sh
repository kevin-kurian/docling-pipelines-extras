#!/bin/sh
set -eu

: "${KAFKA_INPUT_TOPIC:?KAFKA_INPUT_TOPIC environment variable is required}"
: "${KAFKA_NOTIFICATION_TOPIC:?KAFKA_NOTIFICATION_TOPIC environment variable is required}"

printf '\n=== Kafka topics before initialization ===\n'
/opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:29092 --list

for topic in "$KAFKA_INPUT_TOPIC" "$KAFKA_NOTIFICATION_TOPIC"; do
  printf '\n=== Creating topic: %s ===\n' "$topic"
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:29092 --create --topic "$topic" \
    --partitions 1 --replication-factor 1 --if-not-exists
done

printf '\n=== Kafka topics after initialization ===\n'
/opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:29092 --list
