from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProcessRequest(BaseModel):
    """Wire contract for POST /process, matching exactly what the Conversation
    Orchestrator's AgentRuntimeClient sends (PascalCase, via plain System.Text.Json
    defaults - not ASP.NET Core's camelCase "Web" defaults)."""

    model_config = ConfigDict(populate_by_name=True)

    conversation_id: str = Field(alias="ConversationId")
    message_type: str = Field(alias="MessageType")
    text: str | None = Field(default=None, alias="Text")
    journey_stage: str | None = Field(default=None, alias="JourneyStage")
    last_intent: str | None = Field(default=None, alias="LastIntent")


class AgentDecision(BaseModel):
    """Internal representation of the agent's decision; also the schema bound to
    Strands' structured output so the LLM's response is parsed directly into this
    shape rather than free text."""

    intent: str | None = None
    confidence: float = 0.0
    reply_text: str | None = None
    requires_handoff: bool = False
    handoff_reason: str | None = None


class ProcessResponse(BaseModel):
    """Wire contract for the POST /process response, matching exactly what the
    Conversation Orchestrator's AgentRuntimeClient expects to deserialize."""

    model_config = ConfigDict(populate_by_name=True)

    intent: str | None = Field(default=None, alias="Intent")
    confidence: float = Field(default=0.0, alias="Confidence")
    reply_text: str | None = Field(default=None, alias="ReplyText")
    requires_handoff: bool = Field(default=False, alias="RequiresHandoff")
    handoff_reason: str | None = Field(default=None, alias="HandoffReason")

    @classmethod
    def from_decision(cls, decision: AgentDecision) -> ProcessResponse:
        return cls(**decision.model_dump())
