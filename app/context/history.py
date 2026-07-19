from __future__ import annotations

import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_fixed

from app.config import Settings
from app.platform import create_service_token

logger = logging.getLogger(__name__)


async def fetch_recent_history(
    settings: Settings,
    tenant_id: str,
    conversation_id: str,
) -> list[dict]:
    try:
        return await _fetch_with_retry(settings, tenant_id, conversation_id)
    except Exception:
        logger.warning(
            "conversation-memory-service unavailable fetching history for conversation %s",
            conversation_id,
            exc_info=True,
        )
        return []


async def _fetch_with_retry(
    settings: Settings,
    tenant_id: str,
    conversation_id: str,
) -> list[dict]:
    @retry(
        stop=stop_after_attempt(settings.conversation_memory_retry_attempts + 1),
        wait=wait_fixed(0.2),
        reraise=True,
    )
    async def _call() -> list[dict]:
        token = create_service_token(
            settings,
            settings.conversation_memory_service_audience,
            tenant_id,
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": tenant_id,
        }
        async with httpx.AsyncClient(
            base_url=settings.conversation_memory_service_base_url,
            timeout=5.0,
            headers=headers,
        ) as client:
            response = await client.get(
                f"/conversations/{conversation_id}/messages",
                params={"limit": settings.conversation_memory_history_limit},
            )
            response.raise_for_status()
            return response.json()

    return await _call()
