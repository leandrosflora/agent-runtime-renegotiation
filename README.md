# Agent Runtime Renegotiation

Runtime de agente de IA para jornada de renegociação de dívidas via canais conversacionais, como WhatsApp.

Este serviço recebe uma solicitação do `conversation-orchestrator`, executa um agente com Amazon Bedrock via Strands Agents, consulta ferramentas MCP e base de conhecimento quando necessário, retorna uma decisão estruturada e publica evento de processamento no Kafka.

## Visão geral

```mermaid
flowchart LR
    Orchestrator[Conversation Orchestrator] -->|POST /process| Runtime[Agent Runtime Renegotiation]
    Runtime -->|LLM inference| Bedrock[Amazon Bedrock]
    Runtime -->|MCP tools| ToolService[Tool Service MCP]
    Runtime -->|GET /search| Knowledge[Knowledge Service]
    Runtime -->|agent.events| Kafka[(Kafka)]
    Runtime -->|ProcessResponse| Orchestrator
```

## Stack

- Python 3.9+
- FastAPI
- Uvicorn
- Strands Agents
- Amazon Bedrock
- MCP client
- HTTPX
- Tenacity
- Confluent Kafka
- Pytest

## Responsabilidades

- Receber contexto da conversa via `POST /process`.
- Montar prompt com mensagem do cliente, estágio da jornada e última intenção.
- Executar agente especializado em renegociação de dívidas.
- Consultar ferramentas MCP do Tool Service quando disponíveis.
- Consultar base de conhecimento de FAQ, políticas e regras de negócio.
- Retornar decisão estruturada para o orquestrador.
- Forçar handoff quando houver baixa confiança ou falha no runtime/modelo.
- Publicar evento `agent.events` no Kafka.

## Endpoint

### `POST /process`

Contrato usado pelo `conversation-orchestrator`.

#### Request

> O contrato usa nomes em PascalCase para compatibilidade com serialização padrão do `.NET System.Text.Json`.

```json
{
  "ConversationId": "conv-001",
  "MessageType": "Text",
  "Text": "Quero renegociar minha dívida",
  "JourneyStage": "initial",
  "LastIntent": null
}
```

#### Response

```json
{
  "Intent": "renegotiation_request",
  "Confidence": 0.92,
  "ReplyText": "Posso te ajudar com a renegociação. Vou consultar as opções disponíveis para você.",
  "RequiresHandoff": false,
  "HandoffReason": null
}
```

#### Campos principais

| Campo | Direção | Descrição |
|---|---|---|
| `ConversationId` | Request | Identificador da conversa. |
| `MessageType` | Request | Tipo da mensagem recebido pelo canal. |
| `Text` | Request | Texto enviado pelo cliente. |
| `JourneyStage` | Request | Estágio atual da jornada conversacional. |
| `LastIntent` | Request | Última intenção identificada anteriormente. |
| `Intent` | Response | Intenção identificada pelo agente. |
| `Confidence` | Response | Confiança da decisão. |
| `ReplyText` | Response | Resposta sugerida para envio ao cliente. |
| `RequiresHandoff` | Response | Indica transferência para atendimento humano. |
| `HandoffReason` | Response | Motivo do handoff. |

## Regras de decisão

- O agente deve responder de forma estruturada usando o schema `AgentDecision`.
- Se a inferência falhar, o serviço retorna `RequiresHandoff = true` com motivo `agent_runtime_unavailable`.
- Se a confiança ficar abaixo do threshold configurado, o serviço força `RequiresHandoff = true` com motivo `low_confidence`.
- O agente não deve inventar valores, prazos ou condições de renegociação; deve usar ferramentas disponíveis.
- A formalização de acordo exige confirmação explícita do cliente.

## Eventos Kafka

### Tópico: `agent.events`

Publicado após o processamento da mensagem.

```json
{
  "conversation_id": "conv-001",
  "intent": "renegotiation_request",
  "confidence": 0.92,
  "requires_handoff": false,
  "handoff_reason": null
}
```

A publicação Kafka não quebra o endpoint em caso de erro; falhas são registradas em log.

## Configuração

O serviço usa `pydantic-settings`, com suporte a variáveis de ambiente.

| Variável | Default | Descrição |
|---|---:|---|
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-5-sonnet-20241022-v2:0` | Modelo usado no Amazon Bedrock. |
| `BEDROCK_REGION` | `us-east-1` | Região AWS do Bedrock. |
| `TOOL_SERVICE_MCP_URL` | `http://localhost:8400/mcp` | Endpoint MCP do Tool Service. |
| `KNOWLEDGE_SERVICE_BASE_URL` | `http://localhost:8500` | Base URL do Knowledge Service. |
| `KNOWLEDGE_SERVICE_RETRY_ATTEMPTS` | `2` | Tentativas adicionais para busca na base de conhecimento. |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Bootstrap servers do Kafka. |
| `KAFKA_AGENT_EVENTS_TOPIC` | `agent.events` | Tópico de eventos do agente. |
| `CONFIDENCE_THRESHOLD` | `0.6` | Confiança mínima antes de forçar handoff. |

Exemplo:

```bash
export BEDROCK_MODEL_ID="anthropic.claude-3-5-sonnet-20241022-v2:0"
export BEDROCK_REGION="us-east-1"
export TOOL_SERVICE_MCP_URL="http://localhost:8400/mcp"
export KNOWLEDGE_SERVICE_BASE_URL="http://localhost:8500"
export KAFKA_BOOTSTRAP_SERVERS="localhost:9092"
```

## Como executar localmente

### Pré-requisitos

- Python 3.9+
- Credenciais AWS configuradas para acesso ao Amazon Bedrock
- Kafka local em `localhost:9092`
- Tool Service MCP disponível ou indisponível de forma tolerada
- Knowledge Service disponível em `localhost:8500`

### Criar ambiente virtual

```bash
python -m venv .venv
```

Ativar no Windows:

```bash
.venv\Scripts\activate
```

Ativar no Linux/macOS:

```bash
source .venv/bin/activate
```

### Instalar dependências

```bash
pip install -r requirements.txt
```

Para desenvolvimento e testes:

```bash
pip install -r requirements-dev.txt
```

### Subir API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8100 --reload
```

Swagger:

```text
http://localhost:8100/docs
```

## Teste rápido

```bash
curl -X POST http://localhost:8100/process \
  -H "Content-Type: application/json" \
  -d '{
    "ConversationId": "conv-001",
    "MessageType": "Text",
    "Text": "Quero renegociar minha dívida",
    "JourneyStage": "initial",
    "LastIntent": null
  }'
```

## Testes

```bash
pytest
```

O `pyproject.toml` já aponta os testes para a pasta `tests` e configura `asyncio_mode = auto`.

## Estrutura

```text
.
├── app
│   ├── agent
│   │   ├── core.py
│   │   └── prompts.py
│   ├── events
│   │   └── publisher.py
│   ├── tools
│   │   ├── knowledge.py
│   │   └── tool_service.py
│   ├── config.py
│   ├── logging_setup.py
│   ├── main.py
│   └── models.py
├── tests
│   ├── agent
│   ├── events
│   └── tools
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
└── agent-runtime-renegotiation.pyproj
```

## Integrações

### Conversation Orchestrator

Chama `POST /process` enviando contexto da conversa e espera um `ProcessResponse` com intenção, confiança, resposta e decisão de handoff.

### Amazon Bedrock

Usado pelo Strands Agents via `BedrockModel`.

### Tool Service MCP

Fornece ferramentas de negócio para consultar elegibilidade, débitos, simulações e formalização. Se a conexão falhar, o agente segue sem essas tools e registra warning.

### Knowledge Service

Exposto via `GET /search?query=...`, usado para buscar FAQ, políticas e regras de renegociação.

### Kafka

Recebe o evento `agent.events` com o resultado da decisão do agente.

## Observações técnicas

- O runtime é stateless; o estado conversacional vem do orquestrador.
- O contrato HTTP usa aliases PascalCase por compatibilidade com .NET.
- Falhas no Tool Service MCP não derrubam a requisição.
- Falhas no Kafka não derrubam a requisição.
- Baixa confiança força handoff humano.

## Próximos passos sugeridos

- Adicionar Dockerfile e docker-compose local.
- Adicionar health checks para Bedrock, Kafka, Tool Service e Knowledge Service.
- Documentar contrato MCP das ferramentas de renegociação.
- Adicionar exemplos de respostas por intenção.
- Criar pipeline CI para lint, testes e security scan.
