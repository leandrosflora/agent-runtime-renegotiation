from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from strands import Agent
from strands.hooks import AfterToolCallEvent
from strands.models import OpenAIModel

from app.agent.prompts import SYSTEM_PROMPT
from app.config import Settings
from app.models import AgentDecision

logger = logging.getLogger(__name__)

AGENT_RUNTIME_UNAVAILABLE_REASON = "agent_runtime_unavailable"
AGENT_RUNTIME_TIMEOUT_REASON = "agent_runtime_timeout"
LOW_CONFIDENCE_REASON = "low_confidence"

# Substring tool-service-renegotiation's policy.py uses in its denial message
# ("Tool '...' is not allowed from journey stage '...'"), distinguishing a routine
# stage-gated denial (expected mid-sequence, not a real failure) from any other tool
# error (missing identifier, downstream unavailable, etc.).
_STAGE_DENIAL_MARKER = "journey stage"

# reply_text the model wrote assumed it would hand off (e.g. "vou transferir voce..."), so
# clearing requires_handoff alone leaves a reply that's now flatly false - confirmed live, a real
# customer read "aguarde enquanto realizo a transferencia" on every turn even after the override
# started firing. Replace it with an honest, deterministic message instead of trying to salvage
# the model's handoff-flavored prose.
_STAGE_DENIAL_OVERRIDE_REPLY = (
    "Já confirmei parte do seu cadastro. Para continuar com a renegociação, pode me confirmar "
    "que deseja seguir? Assim eu prossigo com os próximos passos."
)

# The governed MCP tools tool-service-renegotiation's policy actually gates by journey stage -
# see policy.py. Excludes search_knowledge_base (ungoverned) and the internal "AgentDecision"
# tool call Strands uses to extract structured_output, neither of which should count towards
# "did the renegotiation sequence make real progress this turn".
_GOVERNED_TOOL_NAMES = frozenset(
    {
        "consultar_cliente",
        "consultar_contratos",
        "consultar_debitos",
        "validar_elegibilidade",
        "simular_proposta",
        "confirmar_acordo",
        "gerar_documento",
    }
)


def build_agent(settings: Settings, tools: list[Any] | None = None) -> Agent:
    model = OpenAIModel(
        client_args={"api_key": settings.openai_api_key},
        model_id=settings.openai_model_id,
        params={"max_tokens": settings.openai_max_tokens},
    )
    return Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools or [])


def _track_tool_outcomes(agent: Agent) -> list[dict[str, Any]]:
    """Registers a hook recording each governed tool call's outcome for this invocation. Feeds
    both the requires_handoff double-check (_override_handoff_for_stage_denial) and the
    JourneyMilestone computation (_compute_journey_milestone) - the same evidence answers "was
    this turn's failure just a routine stage gate" and "what did this turn actually accomplish"."""
    outcomes: list[dict[str, Any]] = []

    def _on_after_tool_call(event: AfterToolCallEvent) -> None:
        tool_name = event.tool_use.get("name") if isinstance(event.tool_use, dict) else None
        if tool_name not in _GOVERNED_TOOL_NAMES:
            return
        tool_input = event.tool_use.get("input") if isinstance(event.tool_use, dict) else None
        result = event.result
        status = result.get("status") if isinstance(result, dict) else None
        text = "".join(
            item.get("text", "")
            for item in (result.get("content") or [])
            if isinstance(item, dict)
        ) if isinstance(result, dict) else ""
        outcomes.append(
            {
                "tool": tool_name,
                "input": tool_input if isinstance(tool_input, dict) else {},
                "result_text": text,
                "success": status == "success",
                "stage_denied": status == "error" and _STAGE_DENIAL_MARKER in text.lower(),
            }
        )

    agent.add_hook(_on_after_tool_call, AfterToolCallEvent)
    return outcomes


def _override_handoff_for_stage_denial(
    decision: AgentDecision, tool_outcomes: list[dict[str, bool]]
) -> AgentDecision:
    """A tool denied only because the journey hasn't reached the required stage yet is expected
    mid-sequence, not a failure - see agent-runtime-renegotiation's app/agent/prompts.py and the
    E2E finding that motivated this. Telling the model not to treat that as a handoff reason
    wasn't reliable on its own (confirmed live: still requested handoff in 2/2 tries with high
    confidence), so this enforces it deterministically: overrides whenever every failure this
    turn was a stage denial, regardless of whether any tool call also succeeded. A success is
    NOT required - confirmed live on the ProposalSelected turn: the customer's raw-text acceptance
    ("Aceito essa proposta") advances the stage only after this turn completes
    (ProposalSelectionDetector, conversation-orchestrator-side), so the agent's premature
    confirmar_acordo attempt is denied with zero governed tool successes this turn, yet it is
    still not a dead end - the very next turn will have the advanced stage and succeed."""
    if not decision.requires_handoff or not tool_outcomes:
        return decision

    any_stage_denied = any(outcome["stage_denied"] for outcome in tool_outcomes)
    any_other_failure = any(
        not outcome["success"] and not outcome["stage_denied"] for outcome in tool_outcomes
    )
    if any_stage_denied and not any_other_failure:
        return decision.model_copy(
            update={
                "requires_handoff": False,
                "handoff_reason": None,
                "reply_text": _STAGE_DENIAL_OVERRIDE_REPLY,
            }
        )

    return decision


# Maps a governed tool's success to the JourneyStage it proves was reached. Values are
# JourneyStage names verbatim (see conversation-orchestrator/Domain/JourneyStage.cs) so the
# Orchestrator can parse JourneyMilestone directly, without a second translation table.
# consultar_debitos is intentionally absent: no JourneyStage represents "debts fetched" on its
# own - it's gated at ContractSelected and doesn't move the journey further by itself.
# confirmar_acordo maps to AgreementConfirmed, not AgreementProcessing - confirmed live:
# tool-service-renegotiation's policy gates gerar_documento behind
# {AgreementConfirmed, DocumentAvailable, Completed}, not AgreementProcessing, and
# confirmar_acordo succeeding IS the confirmation (this mock has no separate async
# "processing" state to represent), so mapping to AgreementProcessing left gerar_documento
# permanently unreachable.
_TOOL_MILESTONES: dict[str, str] = {
    "consultar_cliente": "CustomerIdentified",
    "consultar_contratos": "ContractSelected",  # overridden to ContractSelectionPending below when ambiguous
    "validar_elegibilidade": "EligibilityChecked",
    "simular_proposta": "ProposalAvailable",
    "confirmar_acordo": "AgreementConfirmed",
    "gerar_documento": "DocumentAvailable",
}

# Order matters: later entries take precedence when multiple governed tools succeed in the same
# turn (e.g. a turn that both identifies the customer and fetches their single contract reports
# ContractSelected, not CustomerIdentified).
_MILESTONE_PRECEDENCE = (
    "consultar_cliente",
    "consultar_contratos",
    "validar_elegibilidade",
    "simular_proposta",
    "confirmar_acordo",
    "gerar_documento",
)

_CONTRACT_SCOPED_TOOLS = frozenset({"consultar_debitos", "validar_elegibilidade", "simular_proposta"})


def _compute_journey_milestone(
    tool_outcomes: list[dict[str, Any]],
    incoming_journey_stage: str | None = None,
    resolved_active_contract_id: str | None = None,
) -> str | None:
    """Derives the turn's JourneyMilestone from verified tool outcomes only - never from the
    model's freeform Intent/reply_text. See journey-milestone-reporting spec: this is what lets
    conversation-orchestrator advance the journey stage reliably instead of guessing from
    keywords in text the model wrote with no constrained vocabulary.

    incoming_journey_stage/resolved_active_contract_id exist only to disambiguate the
    multi-contract case (see _contracts_milestone) - every other milestone depends solely on
    tool_outcomes."""
    successes_by_tool: dict[str, dict[str, Any]] = {
        outcome["tool"]: outcome for outcome in tool_outcomes if outcome["success"]
    }

    milestone: str | None = None
    for tool_name in _MILESTONE_PRECEDENCE:
        outcome = successes_by_tool.get(tool_name)
        if outcome is None:
            continue
        if tool_name == "consultar_contratos":
            milestone = _contracts_milestone(
                outcome, tool_outcomes, incoming_journey_stage, resolved_active_contract_id
            )
        else:
            milestone = _TOOL_MILESTONES[tool_name]
    return milestone


def _contracts_milestone(
    contracts_outcome: dict[str, Any],
    tool_outcomes: list[dict[str, Any]],
    incoming_journey_stage: str | None,
    resolved_active_contract_id: str | None,
) -> str:
    """A single contract is unambiguous - always ContractSelected. More than one requires the
    customer to pick, confirmed one of two ways:

    1. A contract-scoped call (consultar_debitos/validar_elegibilidade/simular_proposta)
       succeeded this turn with a contract_id - kept as a defensive/forward-compatible check, but
       in practice tool-service-renegotiation's policy only allows those tools from
       ContractSelected onward, so this can't actually fire while still at
       ContractSelectionPending; it's here in case that policy ever loosens.
    2. The turn started at ContractSelectionPending (the customer was already asked to choose)
       and the model now reports an active_contract_id matching one of the contracts just
       returned - this is the real, reachable path: the customer's reply named one, the model
       resolved which, and that resolution is what this milestone confirms.

    Otherwise ContractSelectionPending, so the agent pauses and asks instead of silently
    guessing which contract to proceed with."""
    contract_count = _count_contracts(contracts_outcome.get("result_text"))
    if contract_count is not None and contract_count <= 1:
        return "ContractSelected"

    for outcome in tool_outcomes:
        if (
            outcome["tool"] in _CONTRACT_SCOPED_TOOLS
            and outcome["success"]
            and outcome.get("input", {}).get("contract_id")
        ):
            return "ContractSelected"

    if incoming_journey_stage == "ContractSelectionPending" and resolved_active_contract_id:
        contract_ids = _contract_ids(contracts_outcome.get("result_text"))
        if resolved_active_contract_id in contract_ids:
            return "ContractSelected"

    return "ContractSelectionPending"


def _count_contracts(result_text: str | None) -> int | None:
    contracts = _parse_contracts(result_text)
    return len(contracts) if contracts is not None else None


def _contract_ids(result_text: str | None) -> set[str]:
    contracts = _parse_contracts(result_text) or []
    return {c.get("contractId") for c in contracts if isinstance(c, dict) and c.get("contractId")}


def _parse_contracts(result_text: str | None) -> list[Any] | None:
    if not result_text:
        return None
    try:
        data = json.loads(result_text)
    except (TypeError, ValueError):
        return None
    contracts = data.get("contracts") if isinstance(data, dict) else None
    return contracts if isinstance(contracts, list) else None


async def invoke_agent(
    agent: Agent,
    text: str | None,
    journey_stage: str | None,
    last_intent: str | None,
    settings: Settings,
    history: list[dict] | None = None,
    active_contract_id: str | None = None,
    active_simulation_id: str | None = None,
    active_agreement_id: str | None = None,
) -> AgentDecision:
    prompt = _build_prompt(
        text,
        journey_stage,
        last_intent,
        history,
        active_contract_id,
        active_simulation_id,
        active_agreement_id,
    )

    tool_outcomes = _track_tool_outcomes(agent)

    try:
        result = await asyncio.wait_for(
            agent.invoke_async(prompt, structured_output_model=AgentDecision),
            timeout=max(1, settings.agent_timeout_seconds),
        )
        decision = result.structured_output
        if decision is None:
            raise ValueError("Agent did not produce a structured decision")
    except TimeoutError:
        logger.error(
            "Agent execution exceeded the hard timeout of %s seconds",
            settings.agent_timeout_seconds,
        )
        return AgentDecision(
            requires_handoff=True,
            handoff_reason=AGENT_RUNTIME_TIMEOUT_REASON,
            reply_text="Nao foi possivel concluir esta etapa com seguranca. Vou transferir o atendimento para um especialista.",
            active_contract_id=active_contract_id,
            active_simulation_id=active_simulation_id,
            active_agreement_id=active_agreement_id,
        )
    except Exception:
        logger.warning("Failed to obtain a decision from the Agent Runtime's model", exc_info=True)
        return AgentDecision(
            requires_handoff=True,
            handoff_reason=AGENT_RUNTIME_UNAVAILABLE_REASON,
            active_contract_id=active_contract_id,
            active_simulation_id=active_simulation_id,
            active_agreement_id=active_agreement_id,
        )

    # Preserve previously persisted state unless the model explicitly returns a replacement.
    decision = decision.model_copy(
        update={
            "active_contract_id": decision.active_contract_id or active_contract_id,
            "active_simulation_id": decision.active_simulation_id or active_simulation_id,
            "active_agreement_id": decision.active_agreement_id or active_agreement_id,
        }
    )

    decision = _override_handoff_for_stage_denial(decision, tool_outcomes)

    decision = decision.model_copy(
        update={
            "journey_milestone": _compute_journey_milestone(
                tool_outcomes, journey_stage, decision.active_contract_id
            )
        }
    )

    if decision.confidence < settings.confidence_threshold:
        decision = decision.model_copy(
            update={
                "requires_handoff": True,
                "handoff_reason": decision.handoff_reason or LOW_CONFIDENCE_REASON,
            }
        )

    return decision


def _build_prompt(
    text: str | None,
    journey_stage: str | None,
    last_intent: str | None,
    history: list[dict] | None = None,
    active_contract_id: str | None = None,
    active_simulation_id: str | None = None,
    active_agreement_id: str | None = None,
) -> str:
    context_lines: list[str] = []
    if journey_stage:
        context_lines.append(f"Estagio atual da jornada: {journey_stage}")
    if last_intent:
        context_lines.append(f"Ultima intencao identificada: {last_intent}")

    # Keep the legacy prompt unchanged when no structured state exists.
    if active_contract_id or active_simulation_id or active_agreement_id:
        state_lines = [
            f"active_contract_id={active_contract_id or 'null'}",
            f"active_simulation_id={active_simulation_id or 'null'}",
            f"active_agreement_id={active_agreement_id or 'null'}",
        ]
        context_lines.append("Estado estruturado da renegociacao:\n" + "\n".join(state_lines))

    if history:
        history_lines = "\n".join(
            f"{message.get('role', '')}: {message.get('content', {}).get('text', '')}"
            for message in history
        )
        context_lines.append(f"Historico recente da conversa:\n{history_lines}")

    context = "\n".join(context_lines)
    message = f"Mensagem do cliente: {text or ''}"
    return f"{context}\n\n{message}" if context else message
