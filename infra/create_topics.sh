#!/usr/bin/env bash
# Create the Kafka topics from the LLD. Run once after `docker compose up -d`.
set -e
KT="docker exec recovery-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092"
$KT --create --if-not-exists --topic subscription-events     --partitions 3 --replication-factor 1
$KT --create --if-not-exists --topic customer-replies        --partitions 3 --replication-factor 1
$KT --create --if-not-exists --topic subscription-events-dlq --partitions 1 --replication-factor 1
$KT --list
