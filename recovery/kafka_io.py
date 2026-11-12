"""Kafka producer / consumer helpers (docs/LLD.md section 2)."""
from __future__ import annotations

import json

from confluent_kafka import Consumer, Producer

from . import config


class PublishError(RuntimeError):
    pass


def make_producer() -> Producer:
    return Producer({
        "bootstrap.servers": config.KAFKA_BOOTSTRAP,
        "acks": "all",                 # wait for the broker to persist it
        "enable.idempotence": True,    # producer retries can't create duplicates
        "linger.ms": 5,
        "message.timeout.ms": 10_000,  # give up after 10 s so the webhook can return 500
    })


def publish(producer: Producer, topic: str, key: str, value: dict, timeout_s: float = 12.0) -> None:
    """Send one message and block until the broker acks it. Raises PublishError otherwise."""
    result = {}

    def on_delivery(err, msg):
        result["err"] = err
        result["done"] = True

    producer.produce(topic, key=key.encode(), value=json.dumps(value).encode(), on_delivery=on_delivery)
    producer.flush(timeout_s)
    if not result.get("done"):
        raise PublishError(f"no ack from Kafka within {timeout_s}s (is Docker running?)")
    if result["err"] is not None:
        raise PublishError(str(result["err"]))


def make_consumer(group_id: str, topics: list[str]) -> Consumer:
    consumer = Consumer({
        "bootstrap.servers": config.KAFKA_BOOTSTRAP,
        "group.id": group_id,
        "enable.auto.commit": False,   # commit only after Postgres + Redis succeeded
        "auto.offset.reset": "earliest",
        "max.poll.interval.ms": 300_000,
    })
    consumer.subscribe(topics)
    return consumer
