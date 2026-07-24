from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__")

    openai_api_key: str = ""
    openai_model_id: str = "gpt-4o-mini"
    openai_max_tokens: int = 600
    agent_timeout_seconds: int = 45
    mock_agent_enabled: bool = False

    tool_service_mcp_url: str = "http://localhost:8400/mcp"
    tool_service_audience: str = "tool-service-renegotiation"

    knowledge_service_base_url: str = "http://localhost:8500"
    knowledge_service_audience: str = "knowledge-service"
    knowledge_service_retry_attempts: int = 2

    conversation_memory_service_base_url: str = "http://localhost:8600"
    conversation_memory_service_audience: str = "conversation-memory-service"
    conversation_memory_history_limit: int = 10
    conversation_memory_retry_attempts: int = 2

    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_agent_events_topic: str = "agent.events"
    confidence_threshold: float = 0.6
    otel_otlp_endpoint: str = "http://localhost:4317"

    internal_auth_enabled: bool = True
    internal_auth_issuer: str = "conversational-ai-platform"
    internal_auth_service_name: str = "agent-runtime-renegotiation"
    internal_auth_outbound_secrets: dict[str, str] = {}
    internal_auth_inbound_secrets: dict[str, str] = {}
    internal_auth_token_ttl_seconds: int = 300


def get_settings() -> Settings:
    return Settings()
