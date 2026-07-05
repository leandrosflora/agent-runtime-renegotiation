import httpx
import pytest
from httpx import ASGITransport

from app.main import app


@pytest.fixture
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def test_valid_request_is_accepted(client: httpx.AsyncClient):
    response = await client.post(
        "/process",
        json={
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
        json={
            "MessageType": "Text",
            "Text": "Ola",
        },
    )

    assert response.status_code == 422


async def test_response_uses_exact_pascal_case_property_names(client: httpx.AsyncClient):
    response = await client.post(
        "/process",
        json={
            "ConversationId": "5511999990000",
            "MessageType": "Text",
            "Text": "Ola",
        },
    )

    body = response.json()
    assert set(body.keys()) == {"Intent", "Confidence", "ReplyText", "RequiresHandoff", "HandoffReason"}
