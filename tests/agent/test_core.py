import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

from app.agent.core import (
    AGENT_RUNTIME_UNAVAILABLE_REASON,
    LOW_CONFIDENCE_REASON,
    _PROPOSAL_ACCEPTED_OVERRIDE_REPLY,
    _STAGE_DENIAL_OVERRIDE_REPLY,
    _compute_journey_milestone,
    _cpf_was_provided_by_customer,
    _customer_selected_proposal,
    _extract_cpf_candidates,
    _override_handoff_for_stage_denial,
    _register_contract_guard,
    _register_identification_guard,
    invoke_agent,
    is_explicit_confirmation_text,
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
    """Simulates the real Agent.add_hook(callback, AfterToolCallEvent) wiring: captures the hooks
    invoke_agent registers, then fires the AfterToolCallEvent ones (with a minimal fake event per
    tool_calls entry) during invoke_async, mirroring the order hooks actually fire in relative to
    the final decision becoming available. Each entry is (tool_name, result_dict, input_dict).

    invoke_agent registers more than one hook (see _track_tool_outcomes and
    _register_identification_guard) - callbacks are captured per event_type, not as a single
    slot, so registering the second hook doesn't silently discard the first."""
    from strands.hooks import AfterToolCallEvent

    agent = MagicMock()
    result = MagicMock()
    result.structured_output = decision
    captured: dict[Any, list] = {}

    def add_hook(callback, event_type=None):
        captured.setdefault(event_type, []).append(callback)

    agent.add_hook.side_effect = add_hook

    async def invoke_async(*args, **kwargs):
        for tool_name, tool_result, tool_input in tool_calls:
            event = AfterToolCallEvent(
                agent=agent,
                selected_tool=None,
                tool_use={"name": tool_name, "input": tool_input, "toolUseId": "t1"},
                invocation_state={},
                result=tool_result,
            )
            for callback in captured.get(AfterToolCallEvent, []):
                callback(event)
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


def test_override_handoff_for_stage_denial_preserves_a_good_reply_when_handoff_not_requested():
    # Reproduces a real case that must NOT be overridden: confirmar_acordo succeeded this turn,
    # then a same-turn gerar_documento attempt was stage-denied only because AgreementConfirmed
    # isn't signed until the *next* turn (the well-established one-turn-behind limitation). The
    # model correctly left requires_handoff=false and wrote an accurate reply describing the real
    # success - unconditionally replacing it here (as an earlier version of this function did)
    # destroyed that accurate "seu acordo foi formalizado" message for no reason.
    decision = AgentDecision(
        requires_handoff=False,
        reply_text="Seu acordo foi formalizado com sucesso! Nao consegui gerar o documento agora.",
    )
    tool_outcomes = [
        {"success": True, "stage_denied": False},
        {"success": False, "stage_denied": True},
    ]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes)

    assert result.reply_text == "Seu acordo foi formalizado com sucesso! Nao consegui gerar o documento agora."
    assert result.requires_handoff is False


def test_override_handoff_for_stage_denial_noop_when_no_tools_were_called():
    decision = AgentDecision(requires_handoff=True, handoff_reason="low_confidence")

    result = _override_handoff_for_stage_denial(decision, [])

    assert result.requires_handoff is True
    assert result.handoff_reason == "low_confidence"


def test_override_handoff_for_stage_denial_uses_proposal_accepted_reply_when_flagged():
    # Reproduces the real "stuck" conversation: customer said "sim" to "Gostaria de seguir com
    # essa proposta?", the model tried confirmar_acordo (stage-denied, ProposalAvailable hadn't
    # advanced to ProposalSelected yet this turn). The generic _STAGE_DENIAL_OVERRIDE_REPLY reads
    # as a non-sequitur here (it re-asks essentially the same question the customer just answered)
    # and never tells the customer the specific word ("confirmo") the next turn's gate needs.
    decision = AgentDecision(requires_handoff=True, handoff_reason="algum motivo")
    tool_outcomes = [{"success": False, "stage_denied": True}]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes, proposal_just_accepted=True)

    assert result.reply_text == _PROPOSAL_ACCEPTED_OVERRIDE_REPLY
    assert "confirmo" in result.reply_text.lower()


def test_override_handoff_for_stage_denial_uses_proposal_accepted_reply_even_when_handoff_not_requested():
    # Reproduces the real bug precisely: live, the model set requires_handoff=false on this turn
    # on its own (so a guard gated on requires_handoff first never even looks at this turn), yet
    # still wrote a confusing reply_text of its own ("houve um erro... revisar a proposta ou
    # simular novas condicoes?") after confirmar_acordo was stage-denied. The customer already
    # accepted the proposal - correcting reply_text can't depend on the model first getting
    # requires_handoff wrong.
    decision = AgentDecision(
        requires_handoff=False,
        reply_text="Parece que houve um erro ao tentar formalizar o acordo.",
    )
    tool_outcomes = [{"success": False, "stage_denied": True}]

    result = _override_handoff_for_stage_denial(decision, tool_outcomes, proposal_just_accepted=True)

    assert result.reply_text == _PROPOSAL_ACCEPTED_OVERRIDE_REPLY
    assert result.requires_handoff is False


def test_override_handoff_for_stage_denial_uses_proposal_accepted_reply_even_without_tool_attempts():
    decision = AgentDecision(requires_handoff=False, reply_text="Obrigado!")

    result = _override_handoff_for_stage_denial(decision, [], proposal_just_accepted=True)

    assert result.reply_text == _PROPOSAL_ACCEPTED_OVERRIDE_REPLY


def test_override_handoff_for_stage_denial_does_not_override_proposal_accepted_reply_on_real_failure():
    decision = AgentDecision(requires_handoff=True, handoff_reason="algum motivo", reply_text="Original")
    tool_outcomes = [{"success": False, "stage_denied": False}]  # e.g. missing simulation_id

    result = _override_handoff_for_stage_denial(decision, tool_outcomes, proposal_just_accepted=True)

    assert result.reply_text == "Original"
    assert result.requires_handoff is True


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


def test_compute_journey_milestone_zero_contracts_is_not_treated_as_selected():
    # Reproduces the real bug: consultar_contratos was called with a truncated/wrong client_id,
    # legitimately succeeded with an empty list, and this used to be misread as "the one obvious
    # contract, already selected" - silently advancing the journey with no real contract_id
    # behind it (structured_state ended up empty).
    outcomes = [_outcome("consultar_contratos", result_text=contracts_result(0)["content"][0]["text"])]

    assert _compute_journey_milestone(outcomes) == "ContractSelectionPending"


def test_compute_journey_milestone_consultar_debitos_alone_has_no_milestone():
    # No JourneyStage represents "debts fetched" on its own - gated at ContractSelected already.
    outcomes = [_outcome("consultar_debitos", input={"contract_id": "c-1"})]

    assert _compute_journey_milestone(outcomes) is None


# --- is_explicit_confirmation_text -------------------------------------------------------------
# Moved from conversation-orchestrator's ExplicitConfirmationDetector (generalize-orchestrator-
# for-multi-agent) - same behavior, now agent-side. Still a curated keyword pattern - see its
# docstring for why proposal selection (below) could move to a model-judged field but this one
# can't without adding a whole extra model round-trip before every turn at this stage.


def test_is_explicit_confirmation_text_positive_match_at_proposal_selected():
    assert is_explicit_confirmation_text("Sim, confirmo o acordo", "ProposalSelected") is True


def test_is_explicit_confirmation_text_positive_match_at_confirmation_pending():
    assert is_explicit_confirmation_text("Pode fechar", "ConfirmationPending") is True


def test_is_explicit_confirmation_text_wrong_stage_returns_false():
    assert is_explicit_confirmation_text("Confirmo", "ProposalAvailable") is False


def test_is_explicit_confirmation_text_negation_returns_false():
    assert is_explicit_confirmation_text("Nao, cancela isso", "ProposalSelected") is False


def test_is_explicit_confirmation_text_no_match_returns_false():
    assert is_explicit_confirmation_text("Quanto fica em 6 vezes?", "ProposalSelected") is False


def test_is_explicit_confirmation_text_none_text_returns_false():
    assert is_explicit_confirmation_text(None, "ProposalSelected") is False


def test_is_explicit_confirmation_text_infinitive_form_matches():
    # Real conversation found stuck on this exact phrasing - "confirmar" (infinitive) matched
    # neither "confirmo" nor "pode confirmar" before the fix.
    assert is_explicit_confirmation_text("Vou confirmar o acordo", "ProposalSelected") is True


# --- _customer_selected_proposal -----------------------------------------------------------------
# Replaces the old ProposalSelectionDetector-style keyword regex (is_proposal_selection_text) with
# the model's own answer to a narrow, closed question (AgentDecision.customer_accepted_proposal) -
# found live to generalize to phrasings ("seguir", "aceitar" as an infinitive) that a fixed keyword
# list kept missing, one at a time, across multiple real conversations in the same day. Also found
# live: that field alone isn't fully reliable when the model is simultaneously attempting
# confirmar_acordo/simular_proposta - a same-turn attempt at confirmar_acordo is used as a second,
# independent signal (see _customer_selected_proposal's docstring).


def _decision(customer_accepted_proposal: bool = False) -> AgentDecision:
    return AgentDecision(
        intent="faq",
        confidence=0.9,
        reply_text="Ok",
        requires_handoff=False,
        customer_accepted_proposal=customer_accepted_proposal,
    )


def test_customer_selected_proposal_true_at_proposal_available():
    assert _customer_selected_proposal(_decision(customer_accepted_proposal=True), "ProposalAvailable", []) is True


def test_customer_selected_proposal_false_when_model_says_no_and_no_tool_attempted():
    assert (
        _customer_selected_proposal(_decision(customer_accepted_proposal=False), "ProposalAvailable", [])
        is False
    )


def test_customer_selected_proposal_false_at_wrong_stage_even_if_model_says_yes():
    # The model's answer only counts while a proposal is actually on the table - if the state has
    # already moved on (or never got there), a stale/misjudged True shouldn't resurrect it.
    assert (
        _customer_selected_proposal(_decision(customer_accepted_proposal=True), "ProposalSelected", [])
        is False
    )


def test_customer_selected_proposal_true_when_confirmar_acordo_was_attempted_even_if_field_is_false():
    # Real conversation found stuck on this exact pattern: the model tried confirmar_acordo (and
    # simular_proposta) in the same turn as "seguir com essa", both denied by stage, but did not
    # also set customer_accepted_proposal=true - leaving the customer stuck re-simulating forever.
    # Attempting confirmar_acordo at all while a proposal is on the table is itself evidence the
    # model believed the customer had accepted it.
    tool_outcomes = [
        {"tool": "confirmar_acordo", "input": {}, "result_text": "not allowed from journey stage", "success": False, "stage_denied": True},
        {"tool": "simular_proposta", "input": {}, "result_text": "not allowed from journey stage", "success": False, "stage_denied": True},
    ]

    result = _customer_selected_proposal(
        _decision(customer_accepted_proposal=False), "ProposalAvailable", tool_outcomes
    )

    assert result is True


def test_customer_selected_proposal_false_when_only_unrelated_tools_were_attempted():
    tool_outcomes = [
        {"tool": "simular_proposta", "input": {}, "result_text": "not allowed from journey stage", "success": False, "stage_denied": True},
    ]

    result = _customer_selected_proposal(
        _decision(customer_accepted_proposal=False), "ProposalAvailable", tool_outcomes
    )

    assert result is False


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

    assert result.state == "ContractSelected"


async def test_invoke_agent_omits_journey_milestone_when_no_tool_succeeded():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi!", requires_handoff=False)
    agent = agent_returning(decision)

    result = await invoke_agent(agent, "Ola", None, None, make_settings())

    assert result.state is None


async def test_invoke_agent_falls_back_to_customer_accepted_proposal_when_no_tool_milestone():
    # No governed tool succeeded this turn (the agent's own confirmar_acordo attempt was
    # stage-denied), but the model judged the customer's message as accepting the proposal -
    # this is what used to live in conversation-orchestrator's ProposalSelectionDetector, then
    # moved agent-side as raw-text regex (generalize-orchestrator-for-multi-agent), now a
    # model-judged structured field instead of a keyword list.
    decision = AgentDecision(
        intent="confirm_agreement_request",
        confidence=0.9,
        reply_text="Ok",
        requires_handoff=False,
        customer_accepted_proposal=True,
    )
    agent = agent_returning(decision)

    result = await invoke_agent(agent, "Bora, pode seguir", "ProposalAvailable", None, make_settings())

    assert result.state == "ProposalSelected"


async def test_invoke_agent_uses_proposal_accepted_reply_when_customer_just_accepted_the_proposal():
    # Reproduces the real bug live: customer said "sim" to "Gostaria de seguir com essa
    # proposta?", the model set customer_accepted_proposal=true and tried confirmar_acordo
    # (stage-denied, since ProposalAvailable hadn't advanced to ProposalSelected yet this turn) -
    # but, as observed live, set requires_handoff=false on its own, with its own confusing
    # reply_text. Before this fix that meant no override applied at all (it was gated on
    # requires_handoff being true first) and the customer saw a message implying something had
    # gone wrong. This asserts the context-aware reply is used regardless.
    decision = AgentDecision(
        intent="present_negotiation_proposal",
        confidence=0.9,
        reply_text="Parece que houve um erro ao tentar formalizar o acordo.",
        requires_handoff=False,
        customer_accepted_proposal=True,
    )
    agent = agent_returning_with_tool_events(
        decision,
        [("confirmar_acordo", stage_denied_result("confirmar_acordo", "ProposalAvailable"), {})],
    )

    result = await invoke_agent(agent, "sim", "ProposalAvailable", None, make_settings())

    assert result.state == "ProposalSelected"
    assert result.requires_handoff is False
    assert result.reply_text == _PROPOSAL_ACCEPTED_OVERRIDE_REPLY


async def test_invoke_agent_advances_when_confirmar_acordo_attempted_without_the_field_set():
    # Reproduces the real stuck conversation: customer said "seguir com essa", the model tried
    # confirmar_acordo AND simular_proposta in the same turn (both denied - ProposalSelected
    # hadn't been reached yet), but did not also set customer_accepted_proposal=true. Without the
    # tool-attempt fallback, this left the customer stuck re-simulating in a loop indefinitely.
    decision = AgentDecision(
        intent="confirm_agreement_request",
        confidence=0.9,
        reply_text="Nao consigo formalizar agora.",
        requires_handoff=False,
        customer_accepted_proposal=False,
    )
    agent = agent_returning_with_tool_events(
        decision,
        [
            ("confirmar_acordo", stage_denied_result("confirmar_acordo", "ProposalAvailable"), {}),
            ("simular_proposta", stage_denied_result("simular_proposta", "ProposalAvailable"), {}),
        ],
    )

    result = await invoke_agent(agent, "seguir com essa", "ProposalAvailable", None, make_settings())

    assert result.state == "ProposalSelected"


async def test_invoke_agent_does_not_advance_when_model_says_customer_did_not_accept():
    decision = AgentDecision(
        intent="faq",
        confidence=0.9,
        reply_text="Claro, aqui esta a resposta.",
        requires_handoff=False,
        customer_accepted_proposal=False,
    )
    agent = agent_returning(decision)

    result = await invoke_agent(agent, "Quanto fica em 6 vezes?", "ProposalAvailable", None, make_settings())

    assert result.state is None


def test_extract_cpf_candidates_from_bare_digits_in_current_message():
    assert _extract_cpf_candidates("meu cpf e 11111111111") == frozenset({"11111111111"})


def test_extract_cpf_candidates_from_punctuated_cpf():
    assert _extract_cpf_candidates("e 111.111.111-11 por favor") == frozenset({"11111111111"})


def test_extract_cpf_candidates_from_user_history_messages():
    history = [
        {"role": "user", "content": {"text": "meu cpf e 22222222222"}},
        {"role": "assistant", "content": {"text": "Ok, obrigado!"}},
    ]

    assert _extract_cpf_candidates("renegociar", history) == frozenset({"22222222222"})


def test_extract_cpf_candidates_ignores_assistant_messages():
    history = [{"role": "assistant", "content": {"text": "seu cpf 33333333333 foi confirmado"}}]

    assert _extract_cpf_candidates("renegociar", history) == frozenset()


def test_extract_cpf_candidates_empty_when_no_cpf_shaped_text():
    assert _extract_cpf_candidates("ola") == frozenset()
    assert _extract_cpf_candidates(None) == frozenset()


def test_cpf_was_provided_by_customer_true_for_a_known_candidate():
    assert _cpf_was_provided_by_customer("11111111111", frozenset({"11111111111"})) is True


def test_cpf_was_provided_by_customer_false_for_a_fabricated_value():
    assert _cpf_was_provided_by_customer("00123456789", frozenset({"11111111111"})) is False


def test_cpf_was_provided_by_customer_false_for_the_literal_placeholder_undefined():
    assert _cpf_was_provided_by_customer("undefined", frozenset({"11111111111"})) is False


def test_cpf_was_provided_by_customer_false_when_no_cpf_was_ever_provided():
    assert _cpf_was_provided_by_customer("00123456789", frozenset()) is False


def _before_tool_call_event(agent: Any, cpf: str | None) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": "consultar_cliente", "input": {"cpf": cpf}, "toolUseId": "t1"},
        invocation_state={},
    )


def test_identification_guard_cancels_a_call_with_a_cpf_the_customer_never_provided():
    # Reproduces the real bug: customer only said "ola"/"renegociar", never a CPF, yet the model
    # called consultar_cliente with a fabricated CPF and got back plausible-looking fake data from
    # core-bancario-mock's generic fallback - nothing downstream would ever have caught that.
    agent = MagicMock()
    _register_identification_guard(agent, frozenset())
    callback = agent.add_hook.call_args.args[0]

    event = _before_tool_call_event(agent, "00123456789")
    callback(event)

    assert event.cancel_tool


def test_identification_guard_cancels_the_literal_placeholder_undefined():
    agent = MagicMock()
    _register_identification_guard(agent, frozenset({"11111111111"}))
    callback = agent.add_hook.call_args.args[0]

    event = _before_tool_call_event(agent, "undefined")
    callback(event)

    assert event.cancel_tool


def test_identification_guard_allows_a_cpf_the_customer_actually_provided():
    agent = MagicMock()
    _register_identification_guard(agent, frozenset({"11111111111"}))
    callback = agent.add_hook.call_args.args[0]

    event = _before_tool_call_event(agent, "11111111111")
    callback(event)

    assert event.cancel_tool is False


def test_identification_guard_ignores_other_tools():
    agent = MagicMock()
    _register_identification_guard(agent, frozenset())
    callback = agent.add_hook.call_args.args[0]

    event = BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": "simular_proposta", "input": {"contract_id": "00123456789-contract-1"}, "toolUseId": "t1"},
        invocation_state={},
    )
    callback(event)

    assert event.cancel_tool is False


def test_identification_guard_cancels_consultar_contratos_with_a_client_id_the_customer_never_provided():
    # Reproduces the real bug: in a multi-contract conversation, the model called
    # consultar_contratos with "2222" - the trailing digits of the client's display name ("Cliente
    # Homologacao 2222") - instead of the real CPF "22222222222" already resolved earlier in the
    # same conversation.
    agent = MagicMock()
    _register_identification_guard(agent, frozenset({"22222222222"}))
    callback = agent.add_hook.call_args.args[0]

    event = BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": "consultar_contratos", "input": {"client_id": "2222"}, "toolUseId": "t1"},
        invocation_state={},
    )
    callback(event)

    assert event.cancel_tool


def test_identification_guard_allows_consultar_contratos_with_the_real_cpf():
    agent = MagicMock()
    _register_identification_guard(agent, frozenset({"22222222222"}))
    callback = agent.add_hook.call_args.args[0]

    event = BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": "consultar_contratos", "input": {"client_id": "22222222222"}, "toolUseId": "t1"},
        invocation_state={},
    )
    callback(event)

    assert event.cancel_tool is False


def test_identification_guard_registers_on_before_tool_call_event():
    agent = MagicMock()
    _register_identification_guard(agent, frozenset())

    agent.add_hook.assert_called_once()
    assert agent.add_hook.call_args.args[1] is BeforeToolCallEvent


async def test_invoke_agent_registers_the_identification_guard_with_hook():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi", requires_handoff=False)
    agent = agent_returning(decision)

    await invoke_agent(agent, "Meu cpf e 11111111111", None, None, make_settings())

    registered_event_types = [call.args[1] for call in agent.add_hook.call_args_list]
    assert BeforeToolCallEvent in registered_event_types


# --- _register_contract_guard -------------------------------------------------------------------


def _after_contract_call_event(agent: Any, contracts: list[dict]) -> AfterToolCallEvent:
    contracts_payload = json.dumps({"found": True, "contracts": contracts})
    return AfterToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": "consultar_contratos", "input": {}, "toolUseId": "t1"},
        invocation_state={},
        result={"status": "success", "content": [{"text": contracts_payload}]},
    )


def _before_contract_scoped_event(agent: Any, tool_name: str, contract_id: str | None) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": tool_name, "input": {"contract_id": contract_id}, "toolUseId": "t1"},
        invocation_state={},
    )


def test_contract_guard_cancels_a_call_with_the_customers_raw_selection_number():
    # Reproduces the real bug: customer replied "1" to a numbered contract list, the model called
    # validar_elegibilidade with the literal string "1" as contract_id instead of resolving it to
    # the real identifier it had just fetched via consultar_contratos in the same turn -
    # renegotiation-service returned a confusing 502 for the nonsense id.
    agent = MagicMock()
    _register_contract_guard(agent, active_contract_id=None)
    after_cb = agent.add_hook.call_args_list[0].args[0]
    before_cb = agent.add_hook.call_args_list[1].args[0]

    after_cb(
        _after_contract_call_event(
            agent, [{"contractId": "22222222222-contract-1"}, {"contractId": "22222222222-contract-2"}]
        )
    )
    event = _before_contract_scoped_event(agent, "validar_elegibilidade", "1")
    before_cb(event)

    assert event.cancel_tool


def test_contract_guard_allows_a_contract_id_seeded_from_active_contract_id():
    agent = MagicMock()
    _register_contract_guard(agent, active_contract_id="11111111111-contract-1")
    before_cb = agent.add_hook.call_args_list[1].args[0]

    event = _before_contract_scoped_event(agent, "validar_elegibilidade", "11111111111-contract-1")
    before_cb(event)

    assert event.cancel_tool is False


def test_contract_guard_allows_a_contract_id_returned_by_consultar_contratos_this_turn():
    agent = MagicMock()
    _register_contract_guard(agent, active_contract_id=None)
    after_cb = agent.add_hook.call_args_list[0].args[0]
    before_cb = agent.add_hook.call_args_list[1].args[0]

    after_cb(_after_contract_call_event(agent, [{"contractId": "22222222222-contract-1"}]))
    event = _before_contract_scoped_event(agent, "simular_proposta", "22222222222-contract-1")
    before_cb(event)

    assert event.cancel_tool is False


def test_contract_guard_ignores_tools_without_a_contract_id_argument():
    agent = MagicMock()
    _register_contract_guard(agent, active_contract_id=None)
    before_cb = agent.add_hook.call_args_list[1].args[0]

    event = BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": "confirmar_acordo", "input": {"simulation_id": "sim-1"}, "toolUseId": "t1"},
        invocation_state={},
    )
    before_cb(event)

    assert event.cancel_tool is False


def test_contract_guard_registers_after_and_before_hooks():
    agent = MagicMock()
    _register_contract_guard(agent, active_contract_id=None)

    registered_event_types = [call.args[1] for call in agent.add_hook.call_args_list]
    assert registered_event_types == [AfterToolCallEvent, BeforeToolCallEvent]


async def test_invoke_agent_registers_the_contract_guard_hooks():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi", requires_handoff=False)
    agent = agent_returning(decision)

    await invoke_agent(agent, "1", None, None, make_settings())

    registered_event_types = [call.args[1] for call in agent.add_hook.call_args_list]
    assert AfterToolCallEvent in registered_event_types
    assert BeforeToolCallEvent in registered_event_types
