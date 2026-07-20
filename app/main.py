import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram

from app.agent.core import build_agent, invoke_agent
from app.agent.mock import build_mock_decision
from app.config import get_settings
from app.context.history import fetch_recent_history
from app.events.publisher import build_producer, publish_agent_event
from app.logging_setup import CorrelationIdMiddleware, configure_logging
from app.models import ProcessRequest, ProcessResponse
from app.platform import PlatformMiddleware, current_tenant_id, metrics_response
from app.tools.knowledge import make_knowledge_base_tool
from app.tools.tool_service import close_tool_service_client, get_tool_service_tools

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()

AGENT_REQUESTS = Counter(
    "agent_runtime_requests_total",
    "Agent processing requests.",
    ["outcome"],
)
AGENT_HANDOFFS = Counter(
    "agent_runtime_handoffs_total",
    "Agent decisions requiring human handoff.",
    ["reason"],
)
AGENT_DURATION = Histogram(
    "agent_runtime_processing_duration_seconds",
    "End-to-end agent processing duration.",
)

_tracer_provider = TracerProvider(
    resource=Resource.create({"service.name": settings.internal_auth_service_name})
)
_tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_otlp_endpoint))
)
trace.set_tracer_provider(_tracer_provider)
HTTPXClientInstrumentor().instrument()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = settings
    app.state.kafka_producer = build_producer(settings)
    yield
    app.state.kafka_producer.flush(5)


app = FastAPI(title="agent-runtime-renegotiation", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    PlatformMiddleware,
    settings=settings,
    public_paths=("/health/live", "/health/ready", "/metrics", "/docs", "/openapi.json", "/redoc"),
    tenant_required_paths=("/process",),
)
FastAPIInstrumentor.instrument_app(app)


@app.exception_handler(RequestValidationError)
async def log_validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.warning("Rejected %s: errors=%s", request.url.path, exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health/live", include_in_schema=False)
async def health_live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready(request: Request) -> JSONResponse:
    runtime_settings = request.app.state.settings
    failures: list[str] = []
    if runtime_settings.internal_auth_enabled:
        for audience in (
            runtime_settings.tool_service_audience,
            runtime_settings.knowledge_service_audience,
            runtime_settings.conversation_memory_service_audience,
        ):
            secret = runtime_settings.internal_auth_outbound_secrets.get(audience)
            if not secret or len(secret.encode("utf-8")) < 32:
                failures.append(f"internal_auth_outbound_secret_missing:{audience}")
        for caller in ("conversation-orchestrator",):
            secret = runtime_settings.internal_auth_inbound_secrets.get(caller)
            if not secret or len(secret.encode("utf-8")) < 32:
                failures.append(f"internal_auth_inbound_secret_missing:{caller}")
    try:
        await asyncio.to_thread(request.app.state.kafka_producer.list_topics, timeout=1)
    except Exception:
        failures.append("kafka_unavailable")
    return JSONResponse(
        {"status": "not_ready" if failures else "ready", "failures": failures},
        status_code=503 if failures else 200,
    )


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


@app.post("/process", response_model=ProcessResponse)
async def process(payload: ProcessRequest, request: Request) -> ProcessResponse:
    runtime_settings = request.app.state.settings
    header_tenant = current_tenant_id()
    if not header_tenant or header_tenant != payload.tenant_id:
        raise HTTPException(status_code=400, detail="Tenant header, signed claim, and payload must match.")

    started = time.perf_counter()
    try:
        if runtime_settings.mock_agent_enabled:
            decision = build_mock_decision(
                text=payload.text,
                journey_stage=payload.journey_stage,
                last_intent=payload.last_intent,
            )
        else:
            history = await fetch_recent_history(
                runtime_settings,
                payload.tenant_id,
                payload.conversation_id,
            )
            mcp_client, tool_service_tools = await get_tool_service_tools(
                runtime_settings,
                payload.tenant_id,
                payload.conversation_id,
                payload.message_id,
                payload.journey_stage,
                payload.journey_version,
                payload.explicit_confirmation_message_id,
            )
            try:
                tools = [
                    *tool_service_tools,
                    make_knowledge_base_tool(runtime_settings, payload.tenant_id),
                ]
                agent = build_agent(runtime_settings, tools=tools)
                decision = await invoke_agent(
                    agent,
                    text=payload.text,
                    journey_stage=payload.journey_stage,
                    last_intent=payload.last_intent,
                    settings=runtime_settings,
                    history=history,
                )
            finally:
                await close_tool_service_client(mcp_client)

        publish_agent_event(
            request.app.state.kafka_producer,
            runtime_settings,
            payload.tenant_id,
            payload.conversation_id,
            decision,
        )
        AGENT_REQUESTS.labels("success").inc()
        if decision.requires_handoff:
            AGENT_HANDOFFS.labels(_normalize_handoff_reason(decision.handoff_reason)).inc()

        logger.info(
            "Processed message for tenant %s conversation %s version %s: intent=%s requires_handoff=%s",
            payload.tenant_id,
            payload.conversation_id,
            payload.journey_version,
            decision.intent,
            decision.requires_handoff,
        )
        return ProcessResponse.from_decision(decision)
    except Exception:
        AGENT_REQUESTS.labels("error").inc()
        raise
    finally:
        AGENT_DURATION.observe(time.perf_counter() - started)


def _normalize_handoff_reason(reason: str | None) -> str:
    known = {
        "agent_runtime_unavailable",
        "low_confidence",
        "customer_requested",
        "policy_denied",
    }
    return reason if reason in known else "other"
