import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.core import (
    AGENT_RUNTIME_UNAVAILABLE_REASON,
    LOW_CONFIDENCE_REASON,
    _STAGE_DENIAL_OVERRIDE_REPLY,
    _compute_journey_milestone,
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


def contracts_result(count: int) -> dict:
    contracts = [{"contractId": f"c-{i}", "productType": "emprestimo_pessoal"} for i in range(count)]
    return {
        "status": "success",
        "content": [{"text": json.dumps({"found": True, "contracts": contracts})}],
    }


def agent_returning_with_tool_events(decision: AgentDecision | None, tool_calls: list[tuple]) -> MagicMock:
    """Simulates the real Agent.add_hook(callback, AfterToolCallEvent) wiring: captures the hook
    invoke_agent registers, then fires it (with a minimal fake event per tool_calls entry) during
    invoke_async, mirroring the order hooks actually fire in relative to the final decision
    becoming available. Each entry is (tool_name, result_dict, input_dict)."""
    from strands.hooks import AfterToolCallEvent

    agent = MagicMock()
    result = MagicMock()
    result.structured_output = decision
    captured: dict = {}

    def add_hook(callback, event_type=None):
        captured["callback"] = callback

    agent.add_hook.side_effect = add_hook

    async def invoke_async(*args, **kwargs):
        for tool_name, tool_result, tool_input in tool_calls:
            captured["callback"](
                AfterToolCallEvent(
                    agent=agent,
                    selected_tool=None,
                    tool_use={"name": tool_name, "input": tool_input, "toolUseId": "t1"},
                    invocation_state={},
                    result=tool_result,
                )
            )
        return result

    agent.invoke_async = AsyncMock(side_effect=invoke_async)
    return agent


def agent_returning_with_tool_results(decision: AgentDecision | None, tool_results: list[dict]) -> MagicMock:
    """Compatibility wrapper for tests that only care about success/stage-denied outcomes, not
    which specific tool ran - the handoff override doesn't look at tool name."""
    return agent_returning_with_tool_events(
        decision, [("consultar_debitos", tool_result, {}) for tool_result in tool_results]
    )


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
    # The model's original reply_text assumed a handoff was happening (e.g. "vou transferir
    # voce...") - leaving it as-is would tell the customer something false. Replaced with an
    # honest, deterministic message instead of trying to salvage the model's prose.
    assert result.reply_text == _STAGE_DENIAL_OVERRIDE_REPLY
    assert "transfer" not in result.reply_text.lower()


def test_override_handoff_for_stage_denial_clears_handoff_when_nothing_succeeded_but_all_denials_were_stage_gated():
    # e.g. the customer's raw-text proposal acceptance hasn't advanced the persisted stage yet
    # (that happens orchestrator-side, after this turn), so the agent's premature confirmar_acordo
    # attempt is denied with zero successes this turn - not a dead end, just one turn early.
    decision = AgentDecision(requires_handoff=True, handoff_reason="algum motivo")
    tool_outcomes = [{"success": False, "stage_denied": True}]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes)

    assert result.requires_handoff is False


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


# --- _compute_journey_milestone -----------------------------------------------------------------


def _outcome(tool: str, *, success: bool = True, stage_denied: bool = False, result_text: str = "", input: dict | None = None) -> dict:
    return {
        "tool": tool,
        "input": input or {},
        "result_text": result_text,
        "success": success,
        "stage_denied": stage_denied,
    }


def test_compute_journey_milestone_single_tool_success():
    outcomes = [_outcome("consultar_cliente")]

    assert _compute_journey_milestone(outcomes) == "CustomerIdentified"


def test_compute_journey_milestone_higher_precedence_wins():
    outcomes = [
        _outcome("consultar_cliente"),
        _outcome("consultar_contratos", result_text=json.dumps({"contracts": [{"contractId": "c-1"}]})),
    ]

    assert _compute_journey_milestone(outcomes) == "ContractSelected"


def test_compute_journey_milestone_none_when_nothing_succeeded():
    outcomes = [_outcome("consultar_cliente", success=False, stage_denied=True)]

    assert _compute_journey_milestone(outcomes) is None


def test_compute_journey_milestone_empty_outcomes():
    assert _compute_journey_milestone([]) is None


def test_compute_journey_milestone_multi_contract_no_selection_is_pending():
    outcomes = [
        _outcome(
            "consultar_contratos",
            result_text=json.dumps({"contracts": [{"contractId": "c-1"}, {"contractId": "c-2"}]}),
        )
    ]

    assert _compute_journey_milestone(outcomes) == "ContractSelectionPending"


def test_compute_journey_milestone_multi_contract_with_scoped_call_is_selected():
    outcomes = [
        _outcome(
            "consultar_contratos",
            result_text=json.dumps({"contracts": [{"contractId": "c-1"}, {"contractId": "c-2"}]}),
        ),
        _outcome("consultar_debitos", input={"contract_id": "c-1"}),
    ]

    assert _compute_journey_milestone(outcomes) == "ContractSelected"


def test_compute_journey_milestone_multi_contract_resolved_after_being_asked():
    # The realistic path: tool-service-renegotiation's policy denies consultar_debitos/
    # validar_elegibilidade/simular_proposta until ContractSelected is already reached, so a
    # scoped call succeeding (the test above) can't actually happen while still at
    # ContractSelectionPending. What really happens: the customer was asked to choose last turn
    # (incoming stage = ContractSelectionPending), and this turn the model resolves their answer
    # into active_contract_id matching one of the contracts just returned again.
    outcomes = [
        _outcome(
            "consultar_contratos",
            result_text=json.dumps({"contracts": [{"contractId": "c-1"}, {"contractId": "c-2"}]}),
        )
    ]

    milestone = _compute_journey_milestone(
        outcomes, incoming_journey_stage="ContractSelectionPending", resolved_active_contract_id="c-1"
    )

    assert milestone == "ContractSelected"


def test_compute_journey_milestone_multi_contract_not_resolved_without_prior_pending_stage():
    # active_contract_id alone isn't enough - it must follow an actual "which one?" turn, or a
    # hallucinated/guessed contract_id would silently skip the selection step.
    outcomes = [
        _outcome(
            "consultar_contratos",
            result_text=json.dumps({"contracts": [{"contractId": "c-1"}, {"contractId": "c-2"}]}),
        )
    ]

    milestone = _compute_journey_milestone(
        outcomes, incoming_journey_stage="IdentificationPending", resolved_active_contract_id="c-1"
    )

    assert milestone == "ContractSelectionPending"


def test_compute_journey_milestone_multi_contract_active_id_must_match_a_returned_contract():
    outcomes = [
        _outcome(
            "consultar_contratos",
            result_text=json.dumps({"contracts": [{"contractId": "c-1"}, {"contractId": "c-2"}]}),
        )
    ]

    milestone = _compute_journey_milestone(
        outcomes, incoming_journey_stage="ContractSelectionPending", resolved_active_contract_id="c-999"
    )

    assert milestone == "ContractSelectionPending"


def test_compute_journey_milestone_single_contract_is_always_selected():
    outcomes = [
        _outcome("consultar_contratos", result_text=json.dumps({"contracts": [{"contractId": "c-1"}]}))
    ]

    assert _compute_journey_milestone(outcomes) == "ContractSelected"


def test_compute_journey_milestone_consultar_debitos_alone_has_no_milestone():
    # No JourneyStage represents "debts fetched" on its own - gated at ContractSelected already.
    outcomes = [_outcome("consultar_debitos", input={"contract_id": "c-1"})]

    assert _compute_journey_milestone(outcomes) is None


# --- invoke_agent + JourneyMilestone wiring ------------------------------------------------------


async def test_invoke_agent_sets_journey_milestone_from_tool_outcomes():
    decision = AgentDecision(intent="consultar_debitos", confidence=0.9, reply_text="Ok", requires_handoff=False)
    agent = agent_returning_with_tool_events(
        decision,
        [
            ("consultar_cliente", success_result(), {}),
            ("consultar_contratos", contracts_result(1), {}),
        ],
    )

    result = await invoke_agent(agent, "Meu CPF e 11111111111", "IdentificationPending", None, make_settings())

    assert result.journey_milestone == "ContractSelected"


async def test_invoke_agent_omits_journey_milestone_when_no_tool_succeeded():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi!", requires_handoff=False)
    agent = agent_returning(decision)

    result = await invoke_agent(agent, "Ola", None, None, make_settings())

    assert result.journey_milestone is None
