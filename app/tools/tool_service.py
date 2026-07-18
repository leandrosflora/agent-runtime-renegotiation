from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.client.streamable_http import streamable_http_client
from strands.tools.mcp import MCPClient

from app.config import Settings
from app.platform import create_service_token

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _authenticated_transport(settings: Settings, tenant_id: str):
    token = create_service_token(settings, settings.tool_service_audience)
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant_id}
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as http_client:
        async with streamable_http_client(
            settings.tool_service_mcp_url,
            http_client=http_client,
        ) as transport:
            yield transport


async def get_tool_service_tools(
    settings: Settings,
    tenant_id: str,
) -> tuple[MCPClient | None, list[Any]]:
    client = MCPClient(lambda: _authenticated_transport(settings, tenant_id))
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
