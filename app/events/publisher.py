from __future__ import annotations

import json
import logging

from confluent_kafka import Producer
from opentelemetry.propagate import inject

from app.config import Settings
from app.models import AgentDecision

logger = logging.getLogger(__name__)


def build_producer(settings: Settings) -> Producer:
    return Producer({"bootstrap.servers": settings.kafka_bootstrap_servers})


def publish_agent_event(
    producer: Producer,
    settings: Settings,
    tenant_id: str,
    conversation_id: str,
    decision: AgentDecision,
) -> None:
    topic = settings.kafka_agent_events_topic
    event = {
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "intent": decision.intent,
        "confidence": decision.confidence,
        "requires_handoff": decision.requires_handoff,
        "handoff_reason": decision.handoff_reason,
    }
    trace_carrier: dict[str, str] = {}
    inject(trace_carrier)
    headers = [(name, value.encode("utf-8")) for name, value in trace_carrier.items()]
    headers.append(("tenant-id", tenant_id.encode("utf-8")))
    try:
        producer.produce(
            topic,
            key=conversation_id.encode("utf-8"),
            value=json.dumps(event).encode("utf-8"),
            headers=headers,
            on_delivery=_make_delivery_callback(conversation_id, topic),
        )
        producer.poll(0)
    except Exception:
        logger.error(
            "Failed to publish agent event for conversation %s to Kafka topic %s",
            conversation_id,
            topic,
            exc_info=True,
        )


def _make_delivery_callback(conversation_id: str, topic: str):
    def _on_delivery(err, _msg) -> None:
        if err is not None:
            logger.error(
                "Kafka delivery failed for conversation %s on topic %s: %s",
                conversation_id,
                topic,
                err,
            )
    return _on_delivery
