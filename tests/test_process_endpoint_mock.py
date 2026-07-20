from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

import app.main as main_module
from app.config import Settings, get_settings
from app.main import app

TENANT_ID = "00000000-0000-0000-0000-000000000001"
HEADERS = {"X-Tenant-Id": TENANT_ID}


@pytest.fixture
async def mock_client():
    app.dependency_overrides = {}
    original_settings = app.state.settings if hasattr(app.state, "settings") else get_settings()
    original_auth_enabled = main_module.settings.internal_auth_enabled
    main_module.settings.internal_auth_enabled = False
    try:
        async with app.router.lifespan_context(app):
            app.state.settings = Settings(mock_agent_enabled=True, internal_auth_enabled=False)
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
    finally:
        app.state.settings = original_settings
        main_module.settings.internal_auth_enabled = original_auth_enabled


async def test_mock_mode_returns_decision_without_calling_openai(mock_client: httpx.AsyncClient):
    response = await mock_client.post(
        "/process",
        headers=HEADERS,
        json={
            "TenantId": TENANT_ID,
            "ConversationId": "5511999990000",
            "MessageId": "wamid.4",
            "MessageType": "Text",
            "Text": "Quero renegociar minha divida",
            "JourneyStage": None,
            "LastIntent": None,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["Intent"] == "renegotiation_request"
    assert body["RequiresHandoff"] is False


async def test_mock_mode_does_not_fetch_conversation_history(
    mock_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    fetch_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(main_module, "fetch_recent_history", fetch_mock)

    await mock_client.post(
        "/process",
        headers=HEADERS,
        json={
            "TenantId": TENANT_ID,
            "ConversationId": "5511999990000",
            "MessageType": "Text",
            "Text": "Quero renegociar minha divida",
        },
    )

    fetch_mock.assert_not_called()
