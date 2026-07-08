from __future__ import annotations

from app.models import AgentDecision

HUMAN_HANDOFF_REASON = "customer_requested_human"

_HUMAN_KEYWORDS = ("atendente", "humano", "pessoa real")
_RENEGOTIATION_KEYWORDS = ("renegoc",)
_DEBT_KEYWORDS = ("divida", "dívida", "debito", "débito", "boleto", "parcela")


def build_mock_decision(text: str | None, journey_stage: str | None, last_intent: str | None) -> AgentDecision:
    """Deterministic stand-in for invoke_agent's OpenAI call, driven by keyword matching
    on the customer's message. Lets the full webhook -> BFF -> orchestrator -> agent runtime
    -> reply pipeline be exercised end-to-end without a real OpenAI API key."""

    normalized = (text or "").lower()

    if any(keyword in normalized for keyword in _HUMAN_KEYWORDS):
        return AgentDecision(
            intent="human_handoff_request",
            confidence=0.99,
            reply_text=None,
            requires_handoff=True,
            handoff_reason=HUMAN_HANDOFF_REASON,
        )

    if any(keyword in normalized for keyword in _RENEGOTIATION_KEYWORDS):
        return AgentDecision(
            intent="renegotiation_request",
            confidence=0.92,
            reply_text="Posso te ajudar com a renegociacao. Vou consultar as opcoes disponiveis para voce.",
            requires_handoff=False,
        )

    if any(keyword in normalized for keyword in _DEBT_KEYWORDS):
        return AgentDecision(
            intent="debt_inquiry",
            confidence=0.88,
            reply_text="Vou consultar os debitos em aberto para voce.",
            requires_handoff=False,
        )

    return AgentDecision(
        intent="greeting",
        confidence=0.8,
        reply_text="Ola! Sou o assistente virtual de renegociacao de dividas. Como posso te ajudar?",
        requires_handoff=False,
    )
