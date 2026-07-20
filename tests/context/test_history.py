import respx
from httpx import Response

from app.config import Settings
from app.context.history import fetch_recent_history

TENANT_ID = "00000000-0000-0000-0000-000000000001"


def make_settings(retry_attempts: int = 1) -> Settings:
    return Settings(
        conversation_memory_service_base_url="http://memory.test",
        conversation_memory_service_audience="conversation-memory-service",
        conversation_memory_history_limit=10,
        conversation_memory_retry_attempts=retry_attempts,
        internal_auth_signing_key="test-signing-key-with-more-than-32-bytes",
    )


@respx.mock
async def test_fetch_recent_history_returns_messages():
    route = respx.get("http://memory.test/conversations/conv-1/messages").mock(
        return_value=Response(200, json=[{"role": "user", "content": {"text": "oi"}}])
    )

    result = await fetch_recent_history(make_settings(), TENANT_ID, "conv-1")

    assert result == [{"role": "user", "content": {"text": "oi"}}]
    assert route.calls.last.request.headers["x-tenant-id"] == TENANT_ID
    assert route.calls.last.request.headers["authorization"].startswith("Bearer ")


@respx.mock
async def test_fetch_recent_history_transient_failure_then_success():
    route = respx.get("http://memory.test/conversations/conv-1/messages")
    route.side_effect = [
        Response(503),
        Response(200, json=[{"role": "assistant", "content": {"text": "ok"}}]),
    ]

    result = await fetch_recent_history(make_settings(retry_attempts=2), TENANT_ID, "conv-1")

    assert result == [{"role": "assistant", "content": {"text": "ok"}}]
    assert route.call_count == 2


@respx.mock
async def test_fetch_recent_history_persistent_failure_returns_empty_list():
    respx.get("http://memory.test/conversations/conv-1/messages").mock(return_value=Response(503))

    result = await fetch_recent_history(make_settings(retry_attempts=1), TENANT_ID, "conv-1")

    assert result == []
