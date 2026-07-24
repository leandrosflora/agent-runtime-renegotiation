from __future__ import annotations

import asyncio
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


def _track_tool_outcomes(agent: Agent) -> list[dict[str, bool]]:
    """Registers a hook recording each tool call's outcome for this invocation, so the model's
    requires_handoff can be double-checked deterministically afterwards instead of trusting prompt
    instructions alone to distinguish a routine stage-gated denial from a real failure."""
    outcomes: list[dict[str, bool]] = []

    def _on_after_tool_call(event: AfterToolCallEvent) -> None:
        tool_name = event.tool_use.get("name") if isinstance(event.tool_use, dict) else None
        if tool_name not in _GOVERNED_TOOL_NAMES:
            return
        result = event.result
        status = result.get("status") if isinstance(result, dict) else None
        text = "".join(
            item.get("text", "")
            for item in (result.get("content") or [])
            if isinstance(item, dict)
        ) if isinstance(result, dict) else ""
        outcomes.append(
            {
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
    confidence), so this enforces it deterministically: only overrides when every failure this
    turn was a stage denial and at least one tool call still succeeded (real progress was made,
    this isn't a dead end)."""
    if not decision.requires_handoff or not tool_outcomes:
        return decision

    any_success = any(outcome["success"] for outcome in tool_outcomes)
    any_stage_denied = any(outcome["stage_denied"] for outcome in tool_outcomes)
    any_other_failure = any(
        not outcome["success"] and not outcome["stage_denied"] for outcome in tool_outcomes
    )
    if any_success and any_stage_denied and not any_other_failure:
        return decision.model_copy(
            update={
                "requires_handoff": False,
                "handoff_reason": None,
                "reply_text": _STAGE_DENIAL_OVERRIDE_REPLY,
            }
        )

    return decision


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
