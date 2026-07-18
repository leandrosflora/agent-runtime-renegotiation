from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.core import (
    AGENT_RUNTIME_UNAVAILABLE_REASON,
    LOW_CONFIDENCE_REASON,
    invoke_agent,
)
from app.config import Settings
from app.models import AgentDecision


def make_settings(confidence_threshold: float = 0.6) -> Settings:
    return Settings(confidence_threshold=confidence_threshold)


def agent_returning(decision: AgentDecision | None) -> MagicMock:
    agent = MagicMock()
    result = MagicMock()
    result.structured_output = decision
    agent.invoke_async = AsyncMock(return_value=result)
    return agent


async def test_invoke_agent_successful_decision_is_returned_unchanged():
    decision = AgentDecision(intent="faq", confidence=0.95, reply_text="Oi!", requires_handoff=False)
    agent = agent_returning(decision)

    result = await invoke_agent(agent, "Ola", None, None, make_settings())

    assert result.intent == "faq"
    assert result.requires_handoff is False


async def test_invoke_agent_model_invocation_failure_returns_fallback_decision():
    agent = MagicMock()
    agent.invoke_async = AsyncMock(side_effect=RuntimeError("no credentials"))

    result = await invoke_agent(agent, "Ola", None, None, make_settings())

    assert result.requires_handoff is True
    assert result.handoff_reason == AGENT_RUNTIME_UNAVAILABLE_REASON
    assert result.intent is None


async def test_invoke_agent_no_structured_output_returns_fallback_decision():
    agent = agent_returning(None)

    result = await invoke_agent(agent, "Ola", None, None, make_settings())

    assert result.requires_handoff is True
    assert result.handoff_reason == AGENT_RUNTIME_UNAVAILABLE_REASON


async def test_invoke_agent_low_confidence_forces_handoff():
    decision = AgentDecision(intent="faq", confidence=0.2, reply_text="Talvez...", requires_handoff=False)
    agent = agent_returning(decision)

    result = await invoke_agent(agent, "Ola", None, None, make_settings(confidence_threshold=0.6))

    assert result.requires_handoff is True
    assert result.handoff_reason == LOW_CONFIDENCE_REASON


async def test_invoke_agent_high_confidence_does_not_force_handoff():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi!", requires_handoff=False)
    agent = agent_returning(decision)

    result = await invoke_agent(agent, "Ola", None, None, make_settings(confidence_threshold=0.6))

    assert result.requires_handoff is False


async def test_invoke_agent_includes_history_in_prompt_when_provided():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi!", requires_handoff=False)
    agent = agent_returning(decision)
    history = [{"role": "user", "content": {"text": "minha divida esta vencida"}}]

    await invoke_agent(agent, "Ola de novo", None, None, make_settings(), history=history)

    prompt = agent.invoke_async.call_args.args[0]
    assert "minha divida esta vencida" in prompt


async def test_invoke_agent_omitted_history_behaves_exactly_as_before():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi!", requires_handoff=False)
    agent = agent_returning(decision)

    await invoke_agent(agent, "Ola", None, None, make_settings())

    prompt = agent.invoke_async.call_args.args[0]
    assert prompt == "Mensagem do cliente: Ola"
