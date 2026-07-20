import respx
from httpx import Response

from app.config import Settings
from app.tools.knowledge import NO_RESULTS_MESSAGE, UNAVAILABLE_MESSAGE, make_knowledge_base_tool


def make_settings(retry_attempts: int = 1) -> Settings:
    return Settings(
        knowledge_service_base_url="http://knowledge.test",
        knowledge_service_retry_attempts=retry_attempts,
        internal_auth_outbound_secrets={"knowledge-service": "test-signing-key-with-more-than-32-bytes"},
    )


@respx.mock
async def test_search_knowledge_base_returns_formatted_results():
    respx.get("http://knowledge.test/search").mock(
        return_value=Response(200, json={"results": [{"title": "FAQ 1", "content": "resposta"}]})
    )
    tool_fn = make_knowledge_base_tool(make_settings(), "00000000-0000-0000-0000-000000000001")

    result = await tool_fn("como funciona a renegociacao?")

    assert "FAQ 1" in result
    assert "resposta" in result


@respx.mock
async def test_search_knowledge_base_no_results():
    respx.get("http://knowledge.test/search").mock(return_value=Response(200, json={"results": []}))
    tool_fn = make_knowledge_base_tool(make_settings(), "00000000-0000-0000-0000-000000000001")

    result = await tool_fn("pergunta sem resposta")

    assert result == NO_RESULTS_MESSAGE


@respx.mock
async def test_search_knowledge_base_transient_failure_then_success():
    route = respx.get("http://knowledge.test/search")
    route.side_effect = [Response(503), Response(200, json={"results": [{"title": "FAQ", "content": "ok"}]})]
    tool_fn = make_knowledge_base_tool(make_settings(retry_attempts=2), "00000000-0000-0000-0000-000000000001")

    result = await tool_fn("pergunta")

    assert "FAQ" in result
    assert route.call_count == 2


@respx.mock
async def test_search_knowledge_base_persistent_failure_returns_unavailable_message():
    respx.get("http://knowledge.test/search").mock(return_value=Response(503))
    tool_fn = make_knowledge_base_tool(make_settings(retry_attempts=1), "00000000-0000-0000-0000-000000000001")

    result = await tool_fn("pergunta")

    assert result == UNAVAILABLE_MESSAGE
