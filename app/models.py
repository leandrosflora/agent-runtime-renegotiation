from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class ProcessRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_id: str = Field(alias="TenantId")
    conversation_id: str = Field(alias="ConversationId")
    message_id: str = Field(alias="MessageId")
    message_type: str = Field(alias="MessageType")
    text: str | None = Field(default=None, alias="Text")
    state: str | None = Field(default=None, alias="State")
    journey_version: int = Field(default=0, ge=0, alias="JourneyVersion")
    last_intent: str | None = Field(default=None, alias="LastIntent")
    # Opaque as far as conversation-orchestrator is concerned (see agent-runtime-orchestration's
    # "Structured state is round-tripped opaquely" requirement) - this service is the one place
    # that knows contract_id/simulation_id/agreement_id are the keys it put there itself.
    structured_state: dict[str, Any] | None = Field(default=None, alias="StructuredState")
    # True only on the turn conversation-orchestrator just found the 15-minute session window
    # expired and reset it - lets the agent tell the customer explicitly instead of silently
    # re-asking for identification as if nothing had happened.
    session_reset: bool = Field(default=False, alias="SessionReset")
    # Start of the current session window - used to discard conversation history from before it,
    # so a reset conversation can't fall back on identity/context from an expired session.
    session_started_at: datetime | None = Field(default=None, alias="SessionStartedAt")


class AgentDecision(BaseModel):
    intent: str | None = None
    confidence: float = 0.0
    reply_text: str | None = None
    requires_handoff: bool = False
    handoff_reason: str | None = None
    # Kept as three named fields internally (not a nested dict) - this is the LLM's own
    # structured-output schema via Strands' structured_output_model, already tuned against real
    # prompt behavior; packed into StructuredState only at the ProcessResponse boundary (see
    # ProcessResponse.from_decision) where conversation-orchestrator's generic contract needs it.
    active_contract_id: str | None = None
    active_simulation_id: str | None = None
    active_agreement_id: str | None = None
    state: str | None = None
    # The model answers this narrow, closed question directly instead of us pattern-matching the
    # customer's raw text afterwards (see core.py's _customer_selected_proposal) - only meaningful
    # when a proposal was presented in a prior turn (state == ProposalAvailable at turn start).
    # Replaced a curated keyword regex that kept having real gaps ("seguir", "aceitar" as an
    # infinitive, ...) - a closed yes/no judgment about one specific message generalizes to
    # phrasings no fixed list could enumerate, without reintroducing the reliability problem the
    # freeform Intent field had (this isn't open classification, it's one narrow binary question).
    customer_accepted_proposal: bool = False
    # Set only on the turn where the model's own reply pivots to a different contract as a
    # fallback (e.g. "nao consegui simular para X, mas Y tambem esta elegivel - deseja seguir com
    # Y?") - distinct from active_contract_id, which still points at the contract actually being
    # processed. Lets a plain affirmative next turn ("sim") be resolved deterministically to the
    # contract that was actually just offered (see core.py's
    # _customer_confirmed_offered_alternative), instead of relying on the model to remember to
    # update active_contract_id itself - confirmed live it doesn't reliably do that.
    offered_alternative_contract_id: str | None = None


class ProcessResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    intent: str | None = Field(default=None, alias="Intent")
    confidence: float = Field(default=0.0, alias="Confidence")
    reply_text: str | None = Field(default=None, alias="ReplyText")
    requires_handoff: bool = Field(default=False, alias="RequiresHandoff")
    handoff_reason: str | None = Field(default=None, alias="HandoffReason")
    state: str | None = Field(default=None, alias="State")
    structured_state: dict[str, Any] | None = Field(default=None, alias="StructuredState")

    @model_serializer(mode="wrap")
    def serialize_compatibly(self, handler):
        data = handler(self)
        for alias, field_name in (
            ("State", "state"),
            ("StructuredState", "structured_state"),
        ):
            if data.get(alias, data.get(field_name)) is None:
                data.pop(alias, None)
                data.pop(field_name, None)
        return data

    @classmethod
    def from_decision(cls, decision: AgentDecision) -> ProcessResponse:
        structured_state = {
            key: value
            for key, value in (
                ("contract_id", decision.active_contract_id),
                ("simulation_id", decision.active_simulation_id),
                ("agreement_id", decision.active_agreement_id),
                ("offered_alternative_contract_id", decision.offered_alternative_contract_id),
            )
            if value is not None
        } or None
        return cls(
            intent=decision.intent,
            confidence=decision.confidence,
            reply_text=decision.reply_text,
            requires_handoff=decision.requires_handoff,
            handoff_reason=decision.handoff_reason,
            state=decision.state,
            structured_state=structured_state,
        )
