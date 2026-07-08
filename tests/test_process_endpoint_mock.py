import httpx
import pytest
from httpx import ASGITransport

from app.config import Settings, get_settings
from app.main import app


@pytest.fixture
async def mock_client():
    app.dependency_overrides = {}
    original_get_settings = get_settings
    try:
        async with app.router.lifespan_context(app):
            app.state.settings = Settings(mock_agent_enabled=True)
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
    finally:
        app.state.settings = original_get_settings()


async def test_mock_mode_returns_decision_without_calling_openai(mock_client: httpx.AsyncClient):
    response = await mock_client.post(
        "/process",
        json={
            "ConversationId": "5511999990000",
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
