from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.core import (
    AGENT_RUNTIME_UNAVAILABLE_REASON,
    LOW_CONFIDENCE_REASON,
    _override_handoff_for_stage_denial,
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


def stage_denied_result(tool_name: str, stage: str) -> dict:
    return {
        "status": "error",
        "content": [{"text": f"Tool '{tool_name}' is not allowed from journey stage '{stage}'."}],
    }


def other_denied_result(message: str) -> dict:
    return {"status": "error", "content": [{"text": message}]}


def success_result() -> dict:
    return {"status": "success", "content": [{"text": "ok"}]}


def agent_returning_with_tool_results(decision: AgentDecision | None, tool_results: list[dict]) -> MagicMock:
    """Simulates the real Agent.add_hook(callback, AfterToolCallEvent) wiring: captures the hook
    invoke_agent registers, then fires it (with a minimal fake event per tool_results entry)
    during invoke_async, mirroring the order hooks actually fire in relative to the final decision
    becoming available."""
    from strands.hooks import AfterToolCallEvent

    agent = MagicMock()
    result = MagicMock()
    result.structured_output = decision
    captured: dict = {}

    def add_hook(callback, event_type=None):
        captured["callback"] = callback

    agent.add_hook.side_effect = add_hook

    async def invoke_async(*args, **kwargs):
        for tool_result in tool_results:
            captured["callback"](
                AfterToolCallEvent(
                    agent=agent,
                    selected_tool=None,
                    tool_use={"name": "consultar_debitos", "input": {}, "toolUseId": "t1"},
                    invocation_state={},
                    result=tool_result,
                )
            )
        return result

    agent.invoke_async = AsyncMock(side_effect=invoke_async)
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


# --- _override_handoff_for_stage_denial -------------------------------------------------------


def test_override_handoff_for_stage_denial_clears_handoff_on_partial_progress():
    decision = AgentDecision(
        intent="transferir para atendimento humano",
        confidence=0.9,
        reply_text="Identifiquei seu cadastro, mas nao consigo consultar os debitos ainda.",
        requires_handoff=True,
        handoff_reason="algum motivo",
    )
    tool_outcomes = [{"success": True, "stage_denied": False}, {"success": False, "stage_denied": True}]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes)

    assert result.requires_handoff is False
    assert result.handoff_reason is None
    assert result.reply_text == decision.reply_text


def test_override_handoff_for_stage_denial_keeps_handoff_when_nothing_succeeded():
    decision = AgentDecision(requires_handoff=True, handoff_reason="algum motivo")
    tool_outcomes = [{"success": False, "stage_denied": True}]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes)

    assert result.requires_handoff is True


def test_override_handoff_for_stage_denial_keeps_handoff_when_a_real_failure_also_happened():
    decision = AgentDecision(requires_handoff=True, handoff_reason="algum motivo")
    tool_outcomes = [
        {"success": True, "stage_denied": False},
        {"success": False, "stage_denied": True},
        {"success": False, "stage_denied": False},  # e.g. missing simulation_id
    ]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes)

    assert result.requires_handoff is True


def test_override_handoff_for_stage_denial_keeps_handoff_when_not_requested():
    decision = AgentDecision(requires_handoff=False)
    tool_outcomes = [{"success": True, "stage_denied": False}, {"success": False, "stage_denied": True}]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes)

    assert result.requires_handoff is False


def test_override_handoff_for_stage_denial_noop_when_no_tools_were_called():
    decision = AgentDecision(requires_handoff=True, handoff_reason="low_confidence")

    result = _override_handoff_for_stage_denial(decision, [])

    assert result.requires_handoff is True
    assert result.handoff_reason == "low_confidence"


# --- invoke_agent + AfterToolCallEvent wiring --------------------------------------------------


async def test_invoke_agent_overrides_handoff_when_only_a_stage_denial_blocked_progress():
    decision = AgentDecision(
        intent="transferir para atendimento humano",
        confidence=0.9,
        reply_text="Identifiquei seu cadastro e localizei seu contrato.",
        requires_handoff=True,
        handoff_reason="algum motivo",
    )
    agent = agent_returning_with_tool_results(
        decision,
        [success_result(), stage_denied_result("consultar_debitos", "IdentificationPending")],
    )

    result = await invoke_agent(agent, "Meu CPF e 11111111111", "IdentificationPending", None, make_settings())

    assert result.requires_handoff is False
    assert result.handoff_reason is None


async def test_invoke_agent_keeps_handoff_when_a_non_stage_denial_also_occurred():
    decision = AgentDecision(requires_handoff=True, handoff_reason="algum motivo")
    agent = agent_returning_with_tool_results(
        decision,
        [success_result(), other_denied_result("simulation_id is required.")],
    )

    result = await invoke_agent(agent, "Confirmo", "ConfirmationPending", None, make_settings())

    assert result.requires_handoff is True
