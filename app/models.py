from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProcessRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_id: str = Field(alias="TenantId")
    conversation_id: str = Field(alias="ConversationId")
    message_id: str = Field(alias="MessageId")
    message_type: str = Field(alias="MessageType")
    text: str | None = Field(default=None, alias="Text")
    journey_stage: str | None = Field(default=None, alias="JourneyStage")
    journey_version: int = Field(default=0, ge=0, alias="JourneyVersion")
    last_intent: str | None = Field(default=None, alias="LastIntent")
    explicit_confirmation_message_id: str | None = Field(
        default=None,
        alias="ExplicitConfirmationMessageId",
    )


class AgentDecision(BaseModel):
    intent: str | None = None
    confidence: float = 0.0
    reply_text: str | None = None
    requires_handoff: bool = False
    handoff_reason: str | None = None


class ProcessResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    intent: str | None = Field(default=None, alias="Intent")
    confidence: float = Field(default=0.0, alias="Confidence")
    reply_text: str | None = Field(default=None, alias="ReplyText")
    requires_handoff: bool = Field(default=False, alias="RequiresHandoff")
    handoff_reason: str | None = Field(default=None, alias="HandoffReason")

    @classmethod
    def from_decision(cls, decision: AgentDecision) -> ProcessResponse:
        return cls(**decision.model_dump())
