from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.models import OpenAIModel

from app.agent.prompts import SYSTEM_PROMPT
from app.config import Settings
from app.models import AgentDecision

logger = logging.getLogger(__name__)

AGENT_RUNTIME_UNAVAILABLE_REASON = "agent_runtime_unavailable"
AGENT_RUNTIME_TIMEOUT_REASON = "agent_runtime_timeout"
LOW_CONFIDENCE_REASON = "low_confidence"

# Substring tool-service-renegotiation's policy.py uses in its denial message
# ("Tool '...' is not allowed from journey stage '...'"), distinguishing a routine
# stage-gated denial (expected mid-sequence, not a real failure) from any other tool
# error (missing identifier, downstream unavailable, etc.).
_STAGE_DENIAL_MARKER = "journey stage"

# reply_text the model wrote assumed it would hand off (e.g. "vou transferir voce..."), so
# clearing requires_handoff alone leaves a reply that's now flatly false - confirmed live, a real
# customer read "aguarde enquanto realizo a transferencia" on every turn even after the override
# started firing. Replace it with an honest, deterministic message instead of trying to salvage
# the model's handoff-flavored prose.
_STAGE_DENIAL_OVERRIDE_REPLY = (
    "Já confirmei parte do seu cadastro. Para continuar com a renegociação, pode me confirmar "
    "que deseja seguir? Assim eu prossigo com os próximos passos."
)

# Used instead of _STAGE_DENIAL_OVERRIDE_REPLY specifically when this turn's denial happened
# because the customer just accepted a proposal (see invoke_agent's proposal_just_accepted).
# Confirmed live: the generic reply above reads as a non-sequitur here - the model's own previous
# turn already asked "Gostaria de seguir com essa proposta?", the customer answered "sim", and the
# generic reply then asks what looks like the exact same question again ("pode me confirmar que
# deseja seguir?"), so the customer reasonably concludes the system didn't register their answer
# and the conversation is stuck. It also never gives the customer a phrase is_explicit_confirmation_text
# will recognize, so even a customer who answers again in good faith (e.g. "sim" again) stays stuck.
# This reply instead makes the state change explicit (proposal accepted) and asks for the specific
# word ("confirmo") the next turn's gate is looking for.
_PROPOSAL_ACCEPTED_OVERRIDE_REPLY = (
    "Certo, entendi que você quer seguir com essa proposta! Para formalizar o acordo com essas "
    "condições, preciso da sua confirmação final: responda \"confirmo\" para eu seguir com a "
    "formalização."
)

# The governed MCP tools tool-service-renegotiation's policy actually gates by journey stage -
# see policy.py. Excludes search_knowledge_base (ungoverned) and the internal "AgentDecision"
# tool call Strands uses to extract structured_output, neither of which should count towards
# "did the renegotiation sequence make real progress this turn".
_GOVERNED_TOOL_NAMES = frozenset(
    {
        "consultar_cliente",
        "consultar_contratos",
        "consultar_debitos",
        "validar_elegibilidade",
        "simular_proposta",
        "confirmar_acordo",
        "gerar_documento",
    }
)


def build_agent(settings: Settings, tools: list[Any] | None = None) -> Agent:
    model = OpenAIModel(
        client_args={"api_key": settings.openai_api_key},
        model_id=settings.openai_model_id,
        params={"max_tokens": settings.openai_max_tokens},
    )
    return Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools or [])


def _track_tool_outcomes(agent: Agent) -> list[dict[str, Any]]:
    """Registers a hook recording each governed tool call's outcome for this invocation. Feeds
    both the requires_handoff double-check (_override_handoff_for_stage_denial) and the
    JourneyMilestone computation (_compute_journey_milestone) - the same evidence answers "was
    this turn's failure just a routine stage gate" and "what did this turn actually accomplish"."""
    outcomes: list[dict[str, Any]] = []

    def _on_after_tool_call(event: AfterToolCallEvent) -> None:
        tool_name = event.tool_use.get("name") if isinstance(event.tool_use, dict) else None
        if tool_name not in _GOVERNED_TOOL_NAMES:
            return
        tool_input = event.tool_use.get("input") if isinstance(event.tool_use, dict) else None
        result = event.result
        status = result.get("status") if isinstance(result, dict) else None
        text = "".join(
            item.get("text", "")
            for item in (result.get("content") or [])
            if isinstance(item, dict)
        ) if isinstance(result, dict) else ""
        outcomes.append(
            {
                "tool": tool_name,
                "input": tool_input if isinstance(tool_input, dict) else {},
                "result_text": text,
                "success": status == "success",
                "stage_denied": status == "error" and _STAGE_DENIAL_MARKER in text.lower(),
            }
        )

    agent.add_hook(_on_after_tool_call, AfterToolCallEvent)
    return outcomes


# Matches an 11-digit CPF written as a bare digit run or with the conventional dot/dash
# punctuation (e.g. "123.456.789-00") - deliberately not a full CPF check-digit validator (that's
# core-bancario-mock's job downstream, and this demo's own reserved test CPFs like "11111111111"
# are all-same-digit values that a real checksum would reject anyway), just a shape loose enough
# to find one if the customer typed it.
_CPF_CANDIDATE_PATTERN = re.compile(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}")

# A Brazilian mobile number is also 11 digits (2-digit DDD + subscriber number), so a bare
# shape-only check can't tell a CPF from a phone number typed nearby. Rather than guessing from
# the digits themselves, exclude a match if the customer's own words right before it name it as a
# phone number - narrow and low-risk: doesn't affect a bare CPF typed with no such context, which
# is the overwhelmingly common case in this conversation.
_PHONE_CONTEXT_WINDOW = 20
_PHONE_CONTEXT_PATTERN = re.compile(r"telefone|celular|whatsapp|\bfone\b|\bcel\b|\btel\b|contato")


def _extract_cpf_candidates(text: str | None, history: list[dict] | None = None) -> frozenset[str]:
    """Every CPF-shaped value the customer has actually typed, in this turn or earlier in the
    conversation - the only values consultar_cliente is allowed to be called with (see
    _register_identification_guard). Only looks at 'user' role messages: an assistant message
    that happens to echo a CPF back doesn't count as the customer providing one."""
    texts = [text or ""]
    if history:
        texts.extend(
            message.get("content", {}).get("text", "")
            for message in history
            if isinstance(message, dict) and message.get("role") == "user"
        )
    candidates: set[str] = set()
    for candidate_text in texts:
        candidate_text = candidate_text or ""
        for match in _CPF_CANDIDATE_PATTERN.finditer(candidate_text):
            digits = re.sub(r"\D", "", match.group())
            if len(digits) != 11:
                continue
            window_start = max(0, match.start() - _PHONE_CONTEXT_WINDOW)
            preceding = _remove_diacritics(candidate_text[window_start : match.start()]).lower()
            if _PHONE_CONTEXT_PATTERN.search(preceding):
                continue
            candidates.add(digits)
    return frozenset(candidates)


def _cpf_was_provided_by_customer(cpf: str | None, cpf_candidates: frozenset[str]) -> bool:
    return re.sub(r"\D", "", cpf or "") in cpf_candidates


# client_id is the same CPF in this system - consultar_contratos calls "/clients/{clientId}/contracts"
# with it directly (see tool-service-renegotiation/app/renegotiation_client.py) - so it must pass
# the same check as consultar_cliente's cpf argument.
_CPF_SCOPED_ARG_TOOLS: dict[str, str] = {
    "consultar_cliente": "cpf",
    "consultar_contratos": "client_id",
}


# consultar_cliente identifies which customer every subsequent tool call in the turn (and the
# conversation) operates on. Confirmed live: faced with a customer who never gave a CPF at all
# (just "renegociar"), the model called it anyway - first with the literal string "undefined",
# then, after that failed, with a fabricated-but-validly-shaped CPF - and core-bancario-mock's
# generic fallback (see IsValidCpfFormat) happily returned plausible-looking fake customer/
# contract/debt data for it, since that fallback exists precisely to answer any well-formed CPF
# that isn't a reserved fixture. Nothing downstream would ever catch this; the agent went on to
# confidently present a stranger's fabricated financial data as the customer's own. Identity
# fabrication is too severe a failure mode to leave to prompt instructions alone (already tried,
# and still not reliable elsewhere in this file - see _override_handoff_for_stage_denial's own
# history), so this blocks it deterministically at the tool-call boundary instead.
#
# Also covers consultar_contratos's client_id, not just consultar_cliente's cpf - confirmed live
# in a multi-contract conversation: on the turn re-confirming which contract the customer picked,
# the model called consultar_contratos with "2222" (the trailing digits of the client's display
# name, "Cliente Homologacao 2222") instead of the real CPF "22222222222" it had already resolved
# earlier in the same conversation. That truncated id doesn't match any real client, so
# core-bancario-mock 404s, which renegotiation-service (correctly) turns into a 200 response with
# an empty contracts list - which _contracts_milestone then misread as "the customer's one and
# only contract, already selected" (see the contract_count fix below), corrupting the journey
# state with no real contract_id behind it.
def _register_identification_guard(agent: Agent, cpf_candidates: frozenset[str]) -> None:
    def _on_before_tool_call(event: BeforeToolCallEvent) -> None:
        if not isinstance(event.tool_use, dict):
            return
        arg_name = _CPF_SCOPED_ARG_TOOLS.get(event.tool_use.get("name"))
        if arg_name is None:
            return
        tool_input = event.tool_use.get("input")
        cpf = tool_input.get(arg_name) if isinstance(tool_input, dict) else None
        if not _cpf_was_provided_by_customer(cpf, cpf_candidates):
            event.cancel_tool = (
                "Esse CPF/client_id nao corresponde a nenhum CPF que o cliente informou nesta "
                "conversa. Use o CPF real que o cliente forneceu - nunca um trecho do nome de "
                "exibicao ou outro valor."
            )

    agent.add_hook(_on_before_tool_call, BeforeToolCallEvent)


# contract_id argument name per tool - see tool-service-renegotiation's app/mcp_server.py.
# confirmar_acordo/gerar_documento take simulation_id/agreement_id instead, not a raw contract_id,
# so they're intentionally excluded here.
_CONTRACT_SCOPED_ARG_TOOLS: dict[str, str] = {
    "consultar_debitos": "contract_id",
    "validar_elegibilidade": "contract_id",
    "simular_proposta": "contract_id",
}


# consultar_debitos/validar_elegibilidade/simular_proposta all take a contract_id the model is
# supposed to resolve from a real consultar_contratos result (or the contract already selected in
# an earlier turn, carried in active_contract_id) - never from the customer's raw selection text.
# Confirmed live: given a customer reply of just "1" (picking the first contract from a numbered
# list), the model called validar_elegibilidade with the literal string "1" as contract_id -
# despite having just fetched the real identifiers via consultar_contratos in the very same turn -
# and renegotiation-service returned a confusing 502 for the nonsense id (see also the
# EligibilityApiClient fix that stopped that specific case from masquerading as an upstream
# outage). Mirrors _register_identification_guard's shape: known_contract_ids seeds from the
# persisted active_contract_id, then grows as consultar_contratos results arrive during this turn.
def _register_contract_guard(agent: Agent, active_contract_id: str | None) -> None:
    known_contract_ids: set[str] = {active_contract_id} if active_contract_id else set()

    def _on_after_tool_call(event: AfterToolCallEvent) -> None:
        if not isinstance(event.tool_use, dict) or event.tool_use.get("name") != "consultar_contratos":
            return
        result = event.result
        text = (
            "".join(
                item.get("text", "")
                for item in (result.get("content") or [])
                if isinstance(item, dict)
            )
            if isinstance(result, dict)
            else ""
        )
        known_contract_ids.update(_contract_ids(text))

    def _on_before_tool_call(event: BeforeToolCallEvent) -> None:
        if not isinstance(event.tool_use, dict):
            return
        arg_name = _CONTRACT_SCOPED_ARG_TOOLS.get(event.tool_use.get("name"))
        if arg_name is None:
            return
        tool_input = event.tool_use.get("input")
        contract_id = tool_input.get(arg_name) if isinstance(tool_input, dict) else None
        if contract_id not in known_contract_ids:
            event.cancel_tool = (
                "Esse contract_id nao corresponde a nenhum contrato real do cliente nesta "
                "conversa. Chame consultar_contratos novamente e use um dos identificadores "
                "retornados - nunca o numero ou texto literal que o cliente digitou."
            )

    agent.add_hook(_on_after_tool_call, AfterToolCallEvent)
    agent.add_hook(_on_before_tool_call, BeforeToolCallEvent)


def _any_non_stage_denial_failure(tool_outcomes: list[dict[str, Any]]) -> bool:
    return any(not outcome["success"] and not outcome["stage_denied"] for outcome in tool_outcomes)


def _override_handoff_for_stage_denial(
    decision: AgentDecision, tool_outcomes: list[dict[str, bool]], proposal_just_accepted: bool = False
) -> AgentDecision:
    """A tool denied only because the journey hasn't reached the required stage yet is expected
    mid-sequence, not a failure - see agent-runtime-renegotiation's app/agent/prompts.py and the
    E2E finding that motivated this. Telling the model not to treat that as a handoff reason
    wasn't reliable on its own (confirmed live: still requested handoff in 2/2 tries with high
    confidence), so this enforces it deterministically: overrides whenever every failure this
    turn was a stage denial, regardless of whether any tool call also succeeded. A success is
    NOT required - confirmed live on the ProposalSelected turn: the customer's raw-text acceptance
    ("Aceito essa proposta") advances the stage only after this turn completes
    (ProposalSelectionDetector, conversation-orchestrator-side), so the agent's premature
    confirmar_acordo attempt is denied with zero governed tool successes this turn, yet it is
    still not a dead end - the very next turn will have the advanced stage and succeed.

    The proposal_just_accepted branch is checked independently of decision.requires_handoff -
    confirmed live the model doesn't reliably set requires_handoff=true on this turn (it often
    writes its own confusing reply_text - "houve um erro... revisar a proposta ou simular novas
    condicoes?" - while leaving requires_handoff=false). This is safe to apply unconditionally
    because proposal_just_accepted, by construction (see invoke_agent), only fires when
    _compute_journey_milestone found no real tool success this turn - there's never a genuine
    success reply at risk of being overwritten.

    The general branch below, unlike that one, IS still gated on decision.requires_handoff. A
    stage denial can legitimately share a turn with a real success (e.g. confirmar_acordo
    succeeding, then a same-turn gerar_documento attempt being denied only because
    AgreementConfirmed isn't signed until next turn) - confirmed live the model's own reply in
    that case ("acordo formalizado com sucesso... mas nao consegui gerar o documento agora") is
    accurate and requires_handoff is correctly left false, so unconditionally replacing it here
    would destroy a good message to fix a problem that didn't exist on that turn. Gating on
    requires_handoff keeps this to the case this override was built for: the model believed it was
    handing off and wrote reply_text assuming that (see _STAGE_DENIAL_OVERRIDE_REPLY's docstring)."""
    if proposal_just_accepted and not _any_non_stage_denial_failure(tool_outcomes):
        return decision.model_copy(
            update={
                "requires_handoff": False,
                "handoff_reason": None,
                "reply_text": _PROPOSAL_ACCEPTED_OVERRIDE_REPLY,
            }
        )

    if not decision.requires_handoff or not tool_outcomes:
        return decision

    any_stage_denied = any(outcome["stage_denied"] for outcome in tool_outcomes)
    any_other_failure = _any_non_stage_denial_failure(tool_outcomes)
    if any_stage_denied and not any_other_failure:
        return decision.model_copy(
            update={
                "requires_handoff": False,
                "handoff_reason": None,
                "reply_text": _STAGE_DENIAL_OVERRIDE_REPLY,
            }
        )

    return decision


# Maps a governed tool's success to the JourneyStage it proves was reached. Values are
# JourneyStage names verbatim (see conversation-orchestrator/Domain/JourneyStage.cs) so the
# Orchestrator can parse JourneyMilestone directly, without a second translation table.
# consultar_debitos is intentionally absent: no JourneyStage represents "debts fetched" on its
# own - it's gated at ContractSelected and doesn't move the journey further by itself.
# confirmar_acordo maps to AgreementConfirmed, not AgreementProcessing - confirmed live:
# tool-service-renegotiation's policy gates gerar_documento behind
# {AgreementConfirmed, DocumentAvailable, Completed}, not AgreementProcessing, and
# confirmar_acordo succeeding IS the confirmation (this mock has no separate async
# "processing" state to represent), so mapping to AgreementProcessing left gerar_documento
# permanently unreachable.
_TOOL_MILESTONES: dict[str, str] = {
    "consultar_cliente": "CustomerIdentified",
    "consultar_contratos": "ContractSelected",  # overridden to ContractSelectionPending below when ambiguous
    "validar_elegibilidade": "EligibilityChecked",
    "simular_proposta": "ProposalAvailable",
    "confirmar_acordo": "AgreementConfirmed",
    "gerar_documento": "DocumentAvailable",
}

# Order matters: later entries take precedence when multiple governed tools succeed in the same
# turn (e.g. a turn that both identifies the customer and fetches their single contract reports
# ContractSelected, not CustomerIdentified).
_MILESTONE_PRECEDENCE = (
    "consultar_cliente",
    "consultar_contratos",
    "validar_elegibilidade",
    "simular_proposta",
    "confirmar_acordo",
    "gerar_documento",
)

_CONTRACT_SCOPED_TOOLS = frozenset({"consultar_debitos", "validar_elegibilidade", "simular_proposta"})


def _compute_journey_milestone(
    tool_outcomes: list[dict[str, Any]],
    incoming_journey_stage: str | None = None,
    resolved_active_contract_id: str | None = None,
) -> str | None:
    """Derives the turn's JourneyMilestone from verified tool outcomes only - never from the
    model's freeform Intent/reply_text. See journey-milestone-reporting spec: this is what lets
    conversation-orchestrator advance the journey stage reliably instead of guessing from
    keywords in text the model wrote with no constrained vocabulary.

    incoming_journey_stage/resolved_active_contract_id exist only to disambiguate the
    multi-contract case (see _contracts_milestone) - every other milestone depends solely on
    tool_outcomes."""
    successes_by_tool: dict[str, dict[str, Any]] = {
        outcome["tool"]: outcome for outcome in tool_outcomes if outcome["success"]
    }

    milestone: str | None = None
    for tool_name in _MILESTONE_PRECEDENCE:
        outcome = successes_by_tool.get(tool_name)
        if outcome is None:
            continue
        if tool_name == "consultar_contratos":
            milestone = _contracts_milestone(
                outcome, tool_outcomes, incoming_journey_stage, resolved_active_contract_id
            )
        else:
            milestone = _TOOL_MILESTONES[tool_name]
    return milestone


def _contracts_milestone(
    contracts_outcome: dict[str, Any],
    tool_outcomes: list[dict[str, Any]],
    incoming_journey_stage: str | None,
    resolved_active_contract_id: str | None,
) -> str:
    """Exactly one contract is unambiguous - always ContractSelected. Zero or more than one
    requires the customer to pick (or, for zero, isn't a real selection at all), confirmed one of
    two ways:

    1. A contract-scoped call (consultar_debitos/validar_elegibilidade/simular_proposta)
       succeeded this turn with a contract_id - kept as a defensive/forward-compatible check, but
       in practice tool-service-renegotiation's policy only allows those tools from
       ContractSelected onward, so this can't actually fire while still at
       ContractSelectionPending; it's here in case that policy ever loosens.
    2. The turn started at ContractSelectionPending (the customer was already asked to choose)
       and the model now reports an active_contract_id matching one of the contracts just
       returned - this is the real, reachable path: the customer's reply named one, the model
       resolved which, and that resolution is what this milestone confirms.

    Otherwise ContractSelectionPending, so the agent pauses and asks instead of silently
    guessing which contract to proceed with. Deliberately contract_count == 1, not <= 1 - confirmed
    live that a truncated/invalid client_id can make consultar_contratos legitimately succeed with
    an empty list (0 contracts), which used to read as "the one obvious contract, already
    selected" here and silently advanced the journey with no real contract_id behind it."""
    contract_count = _count_contracts(contracts_outcome.get("result_text"))
    if contract_count == 1:
        return "ContractSelected"

    for outcome in tool_outcomes:
        if (
            outcome["tool"] in _CONTRACT_SCOPED_TOOLS
            and outcome["success"]
            and outcome.get("input", {}).get("contract_id")
        ):
            return "ContractSelected"

    if incoming_journey_stage == "ContractSelectionPending" and resolved_active_contract_id:
        contract_ids = _contract_ids(contracts_outcome.get("result_text"))
        if resolved_active_contract_id in contract_ids:
            return "ContractSelected"

    return "ContractSelectionPending"


def _count_contracts(result_text: str | None) -> int | None:
    contracts = _parse_contracts(result_text)
    return len(contracts) if contracts is not None else None


def _contract_ids(result_text: str | None) -> set[str]:
    contracts = _parse_contracts(result_text) or []
    return {c.get("contractId") for c in contracts if isinstance(c, dict) and c.get("contractId")}


def _parse_contracts(result_text: str | None) -> list[Any] | None:
    if not result_text:
        return None
    try:
        data = json.loads(result_text)
    except (TypeError, ValueError):
        return None
    contracts = data.get("contracts") if isinstance(data, dict) else None
    return contracts if isinstance(contracts, list) else None


# Moved here from conversation-orchestrator's ExplicitConfirmationDetector/ProposalSelectionDetector
# (generalize-orchestrator-for-multi-agent): once the orchestrator became skill-agnostic it could
# no longer host renegotiation-specific Portuguese regex, and this judgment always belonged to
# whichever agent understands the domain, the same way JourneyMilestone's tool-outcome evidence
# does - it just happens to read the customer's raw text instead of a tool result.
def _remove_diacritics(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped)


_NEGATION_PATTERN = re.compile(r"\b(nao|nunca|cancel|desist)\b")
# Includes the bare infinitive ("aceitar"/"confirmar") alongside the conjugated forms - confirmed
# live a real customer's "vou aceitar essa" matched neither "aceito" nor "aceita" and got stuck
# repeating the stage-denial override reply forever. Deliberately still a curated phrase list, not
# a stem wildcard (e.g. "confirm\w*") - confirmar_acordo is gated by this, and a stem match would
# also fire on a customer just asking "posso confirmar antes?", not actually confirming yet.
_EXPLICIT_CONFIRMATION_PATTERN = re.compile(
    r"\b(confirmo|confirmar|aceito|aceitar|pode confirmar|pode fechar|fechar acordo|quero fechar|sim confirmo|sim aceito)\b"
)


def _matches_customer_text(text: str | None, pattern: re.Pattern[str]) -> bool:
    if not text:
        return False
    normalized = _remove_diacritics(text).lower()
    if _NEGATION_PATTERN.search(normalized):
        return False
    return bool(pattern.search(normalized))


def is_explicit_confirmation_text(text: str | None, state: str | None) -> bool:
    """Mirrors ExplicitConfirmationDetector: only meaningful once a proposal has been selected
    and the customer is confirming *this* turn - used to unlock confirmar_acordo's gate on
    tool-service-renegotiation (see app/tools/tool_service.py's confirmation_message_id claim),
    which must be decided before the agent (and its tools) are even constructed. Still a curated
    keyword pattern, not a model-judged field like _customer_selected_proposal below - unlike
    proposal selection, this must be known *before* the agent runs (it gates a signed claim used
    by that same turn's tool calls), so there's no "ask the model, use its answer" turn order that
    works without adding a whole extra model round-trip before every turn at this stage."""
    if state not in ("ProposalSelected", "ConfirmationPending"):
        return False
    return _matches_customer_text(text, _EXPLICIT_CONFIRMATION_PATTERN)


def _customer_selected_proposal(
    decision: AgentDecision, state: str | None, tool_outcomes: list[dict[str, Any]]
) -> bool:
    """Replaces the old ProposalSelectionDetector-style keyword regex: the model answers this
    narrow, closed question directly (AgentDecision.customer_accepted_proposal) instead of us
    pattern-matching its raw text afterwards. Unlike explicit confirmation above, this doesn't
    gate any tool call - it only feeds the milestone fallback below - so there's no ordering
    constraint forcing a pre-agent check, and the model's answer can be used as-is.

    Confirmed live this field alone isn't fully reliable: on a turn where the model also attempted
    confirmar_acordo/simular_proposta (both stage-denied, since ProposalSelected hadn't been
    reached yet), it did not also set customer_accepted_proposal - leaving the customer stuck. A
    model attempting confirmar_acordo at all while a proposal is still on the table is itself
    strong, verifiable behavioral evidence it believed the customer had accepted it (why else try
    to formalize an agreement?) - used here as a second, independent signal alongside the field,
    from the same tool-outcome tracking the milestone computation above already relies on."""
    if state != "ProposalAvailable":
        return False
    if decision.customer_accepted_proposal:
        return True
    return any(outcome["tool"] == "confirmar_acordo" for outcome in tool_outcomes)


# A plain "sim"/"ok"/similar affirmative isn't itself ambiguous - the ambiguity was always about
# which contract it applies to, not whether it's a yes. Deliberately broad, same spirit as
# _EXPLICIT_CONFIRMATION_PATTERN's list: not trying to enumerate every possible phrasing, just the
# common short ones a customer would actually use to answer a yes/no offer.
_AFFIRMATIVE_PATTERN = re.compile(
    r"\b(sim|isso|esse mesmo|essa mesma|pode ser|topo|beleza|fechado|bora|ok|blz)\b"
)


def _customer_confirmed_offered_alternative(
    text: str | None, offered_alternative_contract_id: str | None
) -> str | None:
    """Reproduces a real multi-contract bug: the model offered Cartao de Credito as a fallback
    after simular_proposta failed for Emprestimo Pessoal, the customer replied "sim", and the
    model kept processing Emprestimo Pessoal anyway - active_contract_id in structured_state never
    actually switched, because nothing forced it to. offered_alternative_contract_id (set by the
    model on the turn it makes the offer - see prompts.py) is what lets this be resolved
    deterministically instead of trusting the model to remember its own pivot: a plain affirmative
    reply to a pending offer means the customer picked the alternative, full stop."""
    if not offered_alternative_contract_id:
        return None
    if not _matches_customer_text(text, _AFFIRMATIVE_PATTERN):
        return None
    return offered_alternative_contract_id


# gerar_documento can never succeed on the same turn confirmar_acordo just did - AgreementConfirmed
# isn't signed into the MCP JWT until the turn after (the same one-turn-behind constraint
# _override_handoff_for_stage_denial documents). That's not a maybe, it's guaranteed every time, so
# the customer shouldn't have to explicitly ask again for what should already be theirs. Appends a
# fixed, deterministic notice instead of leaving it to the model to remember to set that
# expectation on every single turn this exact pattern occurs.
_DOCUMENT_PENDING_SUFFIX = (
    " Seu comprovante ja esta sendo preparado - e so me mandar qualquer mensagem que eu te envio."
)


def _append_document_pending_notice(
    decision: AgentDecision, tool_outcomes: list[dict[str, Any]], milestone: str | None
) -> AgentDecision:
    if milestone != "AgreementConfirmed":
        return decision
    gerar_documento_denied = any(
        outcome["tool"] == "gerar_documento" and not outcome["success"] for outcome in tool_outcomes
    )
    if not gerar_documento_denied or not decision.reply_text:
        return decision
    if _DOCUMENT_PENDING_SUFFIX.strip() in decision.reply_text:
        return decision
    return decision.model_copy(update={"reply_text": decision.reply_text + _DOCUMENT_PENDING_SUFFIX})


async def invoke_agent(
    agent: Agent,
    text: str | None,
    journey_stage: str | None,
    last_intent: str | None,
    settings: Settings,
    history: list[dict] | None = None,
    active_contract_id: str | None = None,
    active_simulation_id: str | None = None,
    active_agreement_id: str | None = None,
    offered_alternative_contract_id: str | None = None,
    session_reset: bool = False,
) -> AgentDecision:
    prompt = _build_prompt(
        text,
        journey_stage,
        last_intent,
        history,
        active_contract_id,
        active_simulation_id,
        active_agreement_id,
        session_reset,
    )

    tool_outcomes = _track_tool_outcomes(agent)
    _register_identification_guard(agent, _extract_cpf_candidates(text, history))
    _register_contract_guard(agent, active_contract_id)

    try:
        result = await asyncio.wait_for(
            agent.invoke_async(prompt, structured_output_model=AgentDecision),
            timeout=max(1, settings.agent_timeout_seconds),
        )
        decision = result.structured_output
        if decision is None:
            raise ValueError("Agent did not produce a structured decision")
    except TimeoutError:
        logger.error(
            "Agent execution exceeded the hard timeout of %s seconds",
            settings.agent_timeout_seconds,
        )
        return AgentDecision(
            requires_handoff=True,
            handoff_reason=AGENT_RUNTIME_TIMEOUT_REASON,
            reply_text="Nao foi possivel concluir esta etapa com seguranca. Vou transferir o atendimento para um especialista.",
            active_contract_id=active_contract_id,
            active_simulation_id=active_simulation_id,
            active_agreement_id=active_agreement_id,
        )
    except Exception:
        logger.warning("Failed to obtain a decision from the Agent Runtime's model", exc_info=True)
        return AgentDecision(
            requires_handoff=True,
            handoff_reason=AGENT_RUNTIME_UNAVAILABLE_REASON,
            active_contract_id=active_contract_id,
            active_simulation_id=active_simulation_id,
            active_agreement_id=active_agreement_id,
        )

    # Preserve previously persisted state unless the model explicitly returns a replacement.
    decision = decision.model_copy(
        update={
            "active_contract_id": decision.active_contract_id or active_contract_id,
            "active_simulation_id": decision.active_simulation_id or active_simulation_id,
            "active_agreement_id": decision.active_agreement_id or active_agreement_id,
        }
    )

    resolved_alt_contract = _customer_confirmed_offered_alternative(text, offered_alternative_contract_id)
    if resolved_alt_contract:
        decision = decision.model_copy(update={"active_contract_id": resolved_alt_contract})

    milestone = _compute_journey_milestone(tool_outcomes, journey_stage, decision.active_contract_id)
    proposal_just_accepted = False
    if milestone is None and _customer_selected_proposal(decision, journey_stage, tool_outcomes):
        milestone = "ProposalSelected"
        proposal_just_accepted = True

    decision = _override_handoff_for_stage_denial(decision, tool_outcomes, proposal_just_accepted)
    decision = decision.model_copy(update={"state": milestone})
    decision = _append_document_pending_notice(decision, tool_outcomes, milestone)

    if decision.confidence < settings.confidence_threshold:
        decision = decision.model_copy(
            update={
                "requires_handoff": True,
                "handoff_reason": decision.handoff_reason or LOW_CONFIDENCE_REASON,
            }
        )

    return decision


def _filter_history_since(history: list[dict] | None, cutoff: datetime | None) -> list[dict]:
    """Drops messages older than cutoff (the current 15-minute session's start) - used every turn,
    not just the reset turn itself, so a conversation can't fall back on identity/context the
    customer provided in an expired session on any later turn either. This is the same history
    list _register_identification_guard reads for CPF candidates, so filtering it here is what
    actually makes a session reset stick - without this, a CPF given minutes before expiry would
    still satisfy that guard after the reset. A message with no parseable createdAt is kept rather
    than dropped, since silently losing real current-session context on a parsing hiccup is worse
    than occasionally keeping one it didn't need to."""
    if not history or cutoff is None:
        return history or []
    kept: list[dict] = []
    for message in history:
        created_at = message.get("createdAt") if isinstance(message, dict) else None
        if created_at is None:
            kept.append(message)
            continue
        try:
            message_time = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            kept.append(message)
            continue
        if message_time >= cutoff:
            kept.append(message)
    return kept


def _build_prompt(
    text: str | None,
    journey_stage: str | None,
    last_intent: str | None,
    history: list[dict] | None = None,
    active_contract_id: str | None = None,
    active_simulation_id: str | None = None,
    active_agreement_id: str | None = None,
    session_reset: bool = False,
) -> str:
    context_lines: list[str] = []
    if session_reset:
        context_lines.append(
            "A sessao anterior deste cliente expirou (mais de 15 minutos) e foi reiniciada agora "
            "- antes de qualquer outra coisa, informe isso ao cliente de forma breve e clara e "
            "peca o CPF novamente, mesmo que ele tenha informado antes."
        )
    if journey_stage:
        context_lines.append(f"Estagio atual da jornada: {journey_stage}")
    if last_intent:
        context_lines.append(f"Ultima intencao identificada: {last_intent}")

    # Keep the legacy prompt unchanged when no structured state exists.
    if active_contract_id or active_simulation_id or active_agreement_id:
        state_lines = [
            f"active_contract_id={active_contract_id or 'null'}",
            f"active_simulation_id={active_simulation_id or 'null'}",
            f"active_agreement_id={active_agreement_id or 'null'}",
        ]
        context_lines.append("Estado estruturado da renegociacao:\n" + "\n".join(state_lines))

    if history:
        history_lines = "\n".join(
            f"{message.get('role', '')}: {message.get('content', {}).get('text', '')}"
            for message in history
        )
        context_lines.append(f"Historico recente da conversa:\n{history_lines}")

    context = "\n".join(context_lines)
    message = f"Mensagem do cliente: {text or ''}"
    return f"{context}\n\n{message}" if context else message
