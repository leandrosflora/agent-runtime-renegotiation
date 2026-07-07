from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__")

    bedrock_model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    bedrock_region: str = "us-east-1"

    # Bypasses Bedrock entirely and returns a canned AgentDecision from keyword matching,
    # for local/E2E testing before real Bedrock credentials are available.
    mock_agent_enabled: bool = False

    tool_service_mcp_url: str = "http://localhost:8400/mcp"

    knowledge_service_base_url: str = "http://localhost:8500"
    knowledge_service_retry_attempts: int = 2

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_agent_events_topic: str = "agent.events"

    confidence_threshold: float = 0.6


def get_settings() -> Settings:
    return Settings()
