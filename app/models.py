from __future__ import annotations

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
