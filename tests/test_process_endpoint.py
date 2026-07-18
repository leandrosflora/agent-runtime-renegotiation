from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

import app.main as main_module
from app.main import app

TENANT_ID = "00000000-0000-0000-0000-000000000001"
HEADERS = {"X-Tenant-Id": TENANT_ID}


@pytest.fixture
async def client():
    original_auth_enabled = main_module.settings.internal_auth_enabled
    main_module.settings.internal_auth_enabled = False
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
    finally:
        main_module.settings.internal_auth_enabled = original_auth_enabled


async def test_valid_request_is_accepted(client: httpx.AsyncClient):
    response = await client.post(
        "/process",
        headers=HEADERS,
        json={
            "TenantId": TENANT_ID,
            "ConversationId": "5511999990000",
            "MessageType": "Text",
            "Text": "Ola, quero renegociar",
            "JourneyStage": "started",
            "LastIntent": None,
        },
    )
    assert response.status_code == 200


async def test_missing_conversation_id_is_rejected(client: httpx.AsyncClient):
    response = await client.post(
        "/process",
        headers=HEADERS,
        json={
            "TenantId": TENANT_ID,
            "MessageType": "Text",
            "Text": "Ola",
        },
    )
    assert response.status_code == 422


async def test_response_uses_exact_pascal_case_property_names(client: httpx.AsyncClient):
    response = await client.post(
        "/process",
        headers=HEADERS,
        json={
            "TenantId": TENANT_ID,
            "ConversationId": "5511999990000",
            "MessageType": "Text",
            "Text": "Ola",
        },
    )
    body = response.json()
    assert set(body.keys()) == {"Intent", "Confidence", "ReplyText", "RequiresHandoff", "HandoffReason"}


async def test_real_path_fetches_conversation_history(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    fetch_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(main_module, "fetch_recent_history", fetch_mock)

    await client.post(
        "/process",
        headers=HEADERS,
        json={
            "TenantId": TENANT_ID,
            "ConversationId": "5511999990000",
            "MessageType": "Text",
            "Text": "Ola, quero renegociar",
        },
    )

    fetch_mock.assert_called_once()
    assert fetch_mock.call_args.args[1] == TENANT_ID
    assert fetch_mock.call_args.args[2] == "5511999990000"
