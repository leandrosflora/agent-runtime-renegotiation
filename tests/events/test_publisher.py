import json
from unittest.mock import MagicMock

from app.config import Settings
from app.events.publisher import publish_agent_event
from app.models import AgentDecision


def make_settings() -> Settings:
    return Settings(kafka_agent_events_topic="agent.events")


def test_publish_agent_event_success_produces_keyed_message():
    producer = MagicMock()
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi!", requires_handoff=False)

    publish_agent_event(producer, make_settings(), "5511999990000", decision)

    producer.produce.assert_called_once()
    _, kwargs = producer.produce.call_args
    assert producer.produce.call_args.args[0] == "agent.events"
    assert kwargs["key"] == b"5511999990000"
    payload = json.loads(kwargs["value"])
    assert payload["intent"] == "faq"
    assert payload["conversation_id"] == "5511999990000"
    producer.poll.assert_called_once_with(0)


def test_publish_agent_event_published_for_fallback_decision():
    producer = MagicMock()
    decision = AgentDecision(requires_handoff=True, handoff_reason="agent_runtime_unavailable")

    publish_agent_event(producer, make_settings(), "5511999990000", decision)

    producer.produce.assert_called_once()
    _, kwargs = producer.produce.call_args
    payload = json.loads(kwargs["value"])
    assert payload["requires_handoff"] is True
    assert payload["handoff_reason"] == "agent_runtime_unavailable"


def test_publish_agent_event_broker_unavailable_does_not_raise():
    producer = MagicMock()
    producer.produce.side_effect = RuntimeError("broker unavailable")
    decision = AgentDecision(intent="faq", confidence=0.9)

    publish_agent_event(producer, make_settings(), "5511999990000", decision)  # should not raise
