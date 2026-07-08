from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__")

    openai_api_key: str = ""
    openai_model_id: str = "gpt-4o-mini"

    # Caps output tokens per OpenAI completion call. Each tool-calling round trip and the
    # final reply are separate completions, so this bounds latency per round trip rather
    # than the whole conversation - lower it further only if replies still run long.
    openai_max_tokens: int = 600

    # Bypasses the LLM entirely and returns a canned AgentDecision from keyword matching,
    # for local/E2E testing before a real OpenAI API key is available.
    mock_agent_enabled: bool = False

    tool_service_mcp_url: str = "http://localhost:8400/mcp"

    knowledge_service_base_url: str = "http://localhost:8500"
    knowledge_service_retry_attempts: int = 2

    # 9092 is Kafka's PLAINTEXT listener, advertised as "kafka:9092" (only resolvable inside
    # the Docker network); 29092 is the EXTERNAL listener, advertised as "localhost:29092",
    # for this service running on the host (e.g. via `uvicorn` outside Docker).
    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_agent_events_topic: str = "agent.events"

    confidence_threshold: float = 0.6


def get_settings() -> Settings:
    return Settings()
