from unittest.mock import MagicMock, patch

from app.config import Settings
from app.tools.tool_service import close_tool_service_client, get_tool_service_tools


def make_settings() -> Settings:
    return Settings(tool_service_mcp_url="http://localhost:8400/mcp")


async def test_get_tool_service_tools_success_returns_client_and_tools():
    fake_client = MagicMock()
    fake_client.list_tools_sync.return_value = ["tool-a", "tool-b"]

    with patch("app.tools.tool_service.MCPClient", return_value=fake_client):
        client, tools = await get_tool_service_tools(make_settings())

    assert client is fake_client
    assert tools == ["tool-a", "tool-b"]
    fake_client.start.assert_called_once()


async def test_get_tool_service_tools_connection_failure_returns_empty_list():
    fake_client = MagicMock()
    fake_client.start.side_effect = RuntimeError("connection refused")

    with patch("app.tools.tool_service.MCPClient", return_value=fake_client):
        client, tools = await get_tool_service_tools(make_settings())

    assert client is None
    assert tools == []


async def test_close_tool_service_client_none_is_noop():
    await close_tool_service_client(None)


async def test_close_tool_service_client_calls_stop():
    fake_client = MagicMock()

    await close_tool_service_client(fake_client)

    fake_client.stop.assert_called_once_with(None, None, None)


async def test_close_tool_service_client_stop_failure_does_not_raise():
    fake_client = MagicMock()
    fake_client.stop.side_effect = RuntimeError("boom")

    await close_tool_service_client(fake_client)
