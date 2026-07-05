from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.client.streamable_http import streamable_http_client
from strands.tools.mcp import MCPClient

from app.config import Settings

logger = logging.getLogger(__name__)


async def get_tool_service_tools(settings: Settings) -> tuple[MCPClient | None, list[Any]]:
    """Connects to the Tool Service's MCP endpoint and lists its tools for this request.

    Returns (client, tools). If the connection fails, returns (None, []) so the agent
    can proceed without Tool Service tools rather than failing the request. The caller
    is responsible for passing the returned client to close_tool_service_client when done.
    """
    client = MCPClient(lambda: streamable_http_client(settings.tool_service_mcp_url))
    try:
        await asyncio.to_thread(client.start)
        tools = await asyncio.to_thread(client.list_tools_sync)
        return client, list(tools)
    except Exception:
        logger.warning("Failed to connect to the Tool Service MCP endpoint", exc_info=True)
        return None, []


async def close_tool_service_client(client: MCPClient | None) -> None:
    if client is None:
        return
    try:
        await asyncio.to_thread(client.stop, None, None, None)
    except Exception:
        logger.warning("Failed to cleanly close the Tool Service MCP client", exc_info=True)
