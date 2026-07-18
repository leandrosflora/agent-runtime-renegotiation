from __future__ import annotations

import logging
from typing import Any

from strands import Agent
from strands.models import OpenAIModel

from app.agent.prompts import SYSTEM_PROMPT
from app.config import Settings
from app.models import AgentDecision

logger = logging.getLogger(__name__)

AGENT_RUNTIME_UNAVAILABLE_REASON = "agent_runtime_unavailable"
LOW_CONFIDENCE_REASON = "low_confidence"


def build_agent(settings: Settings, tools: list[Any] | None = None) -> Agent:
    model = OpenAIModel(
        client_args={"api_key": settings.openai_api_key},
        model_id=settings.openai_model_id,
        params={"max_tokens": settings.openai_max_tokens},
    )
    return Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools or [])


async def invoke_agent(
    agent: Agent,
    text: str | None,
    journey_stage: str | None,
    last_intent: str | None,
    settings: Settings,
    history: list[dict] | None = None,
) -> AgentDecision:
    prompt = _build_prompt(text, journey_stage, last_intent, history)

    try:
        result = await agent.invoke_async(prompt, structured_output_model=AgentDecision)
        decision = result.structured_output
        if decision is None:
            raise ValueError("Agent did not produce a structured decision")
    except Exception:
        logger.warning("Failed to obtain a decision from the Agent Runtime's model", exc_info=True)
        return AgentDecision(requires_handoff=True, handoff_reason=AGENT_RUNTIME_UNAVAILABLE_REASON)

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
) -> str:
    context_lines: list[str] = []
    if journey_stage:
        context_lines.append(f"Estagio atual da jornada: {journey_stage}")
    if last_intent:
        context_lines.append(f"Ultima intencao identificada: {last_intent}")

    if history:
        history_lines = "\n".join(
            f"{message.get('role', '')}: {message.get('content', {}).get('text', '')}"
            for message in history
        )
        context_lines.append(f"Historico recente da conversa:\n{history_lines}")

    context = "\n".join(context_lines)
    message = f"Mensagem do cliente: {text or ''}"
    return f"{context}\n\n{message}" if context else message
