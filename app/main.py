import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.agent.core import build_agent, invoke_agent
from app.config import get_settings
from app.events.publisher import build_producer, publish_agent_event
from app.logging_setup import CorrelationIdMiddleware, configure_logging
from app.models import ProcessRequest, ProcessResponse
from app.tools.knowledge import make_knowledge_base_tool
from app.tools.tool_service import close_tool_service_client, get_tool_service_tools

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.kafka_producer = build_producer(settings)
    yield
    app.state.kafka_producer.flush(5)


app = FastAPI(title="agent-runtime-renegotiation", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)


@app.post("/process", response_model=ProcessResponse)
async def process(payload: ProcessRequest, request: Request) -> ProcessResponse:
    settings = request.app.state.settings

    mcp_client, tool_service_tools = await get_tool_service_tools(settings)
    try:
        tools = [*tool_service_tools, make_knowledge_base_tool(settings)]
        agent = build_agent(settings, tools=tools)

        decision = await invoke_agent(
            agent,
            text=payload.text,
            journey_stage=payload.journey_stage,
            last_intent=payload.last_intent,
            settings=settings,
        )
    finally:
        await close_tool_service_client(mcp_client)

    publish_agent_event(request.app.state.kafka_producer, settings, payload.conversation_id, decision)

    logger.info(
        "Processed message for conversation %s: intent=%s requires_handoff=%s",
        payload.conversation_id,
        decision.intent,
        decision.requires_handoff,
    )

    return ProcessResponse.from_decision(decision)
