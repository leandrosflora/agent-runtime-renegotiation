from __future__ import annotations

import logging
from typing import Any

import httpx
from strands import tool
from tenacity import retry, stop_after_attempt, wait_fixed

from app.config import Settings
from app.platform import create_service_token

logger = logging.getLogger(__name__)
UNAVAILABLE_MESSAGE = "Base de conhecimento indisponivel no momento."
NO_RESULTS_MESSAGE = "Nenhum resultado encontrado na base de conhecimento para essa pergunta."


def make_knowledge_base_tool(settings: Settings, tenant_id: str) -> Any:
    @tool
    async def search_knowledge_base(query: str) -> str:
        """Busca contexto de FAQ, politicas e regras de negocio da tenant atual."""
        try:
            results = await _search_with_retry(settings, tenant_id, query)
        except Exception:
            logger.warning("Knowledge Service unavailable after retries", exc_info=True)
            return UNAVAILABLE_MESSAGE
        if not results:
            return NO_RESULTS_MESSAGE
        return "\n".join(f"- {item.get('title', '')}: {item.get('content', '')}" for item in results)
    return search_knowledge_base


async def _search_with_retry(settings: Settings, tenant_id: str, query: str) -> list[dict]:
    @retry(
        stop=stop_after_attempt(settings.knowledge_service_retry_attempts + 1),
        wait=wait_fixed(0.2),
        reraise=True,
    )
    async def _call() -> list[dict]:
        token = create_service_token(settings, settings.knowledge_service_audience)
        headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant_id}
        async with httpx.AsyncClient(
            base_url=settings.knowledge_service_base_url,
            timeout=5.0,
            headers=headers,
        ) as client:
            response = await client.get("/search", params={"query": query})
            response.raise_for_status()
            return response.json().get("results", [])
    return await _call()
