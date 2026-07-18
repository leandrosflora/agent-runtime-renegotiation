from __future__ import annotations

import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_fixed

from app.config import Settings

logger = logging.getLogger(__name__)


async def fetch_recent_history(settings: Settings, conversation_id: str) -> list[dict]:
    """Fetches recent conversation history from conversation-memory-service.

    Never raises: a retry-exhausted failure is logged and treated as "no history
    available" (empty list) rather than propagated, since there is no caller here (unlike
    a Strands tool) positioned to react to an error - this runs before the agent exists
    for the current request.
    """
    try:
        return await _fetch_with_retry(settings, conversation_id)
    except Exception:
        logger.warning(
            "conversation-memory-service unavailable fetching history for conversation %s",
            conversation_id,
            exc_info=True,
        )
        return []


async def _fetch_with_retry(settings: Settings, conversation_id: str) -> list[dict]:
    @retry(
        stop=stop_after_attempt(settings.conversation_memory_retry_attempts + 1),
        wait=wait_fixed(0.2),
        reraise=True,
    )
    async def _call() -> list[dict]:
        async with httpx.AsyncClient(
            base_url=settings.conversation_memory_service_base_url, timeout=5.0
        ) as client:
            response = await client.get(
                f"/conversations/{conversation_id}/messages",
                params={
                    "tenant_id": settings.conversation_memory_tenant_id,
                    "limit": settings.conversation_memory_history_limit,
                },
            )
            response.raise_for_status()
            return response.json()

    return await _call()
