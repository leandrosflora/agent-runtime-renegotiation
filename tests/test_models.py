from app.models import AgentDecision, ProcessResponse


def test_from_decision_packs_active_ids_into_structured_state():
    decision = AgentDecision(
        intent="simular_proposta",
        confidence=0.9,
        reply_text="Ok",
        requires_handoff=False,
        active_contract_id="contract-1",
        active_simulation_id="sim-1",
        state="ProposalAvailable",
    )

    response = ProcessResponse.from_decision(decision)

    assert response.structured_state == {"contract_id": "contract-1", "simulation_id": "sim-1"}
    assert response.state == "ProposalAvailable"


def test_from_decision_omits_structured_state_when_no_ids_present():
    decision = AgentDecision(intent="faq", confidence=0.9, reply_text="Oi!", requires_handoff=False)

    response = ProcessResponse.from_decision(decision)

    assert response.structured_state is None


def test_process_response_serializes_with_pascal_case_aliases_and_omits_none_fields():
    decision = AgentDecision(
        intent="faq",
        confidence=0.9,
        reply_text="Oi!",
        requires_handoff=False,
        active_agreement_id="agr-1",
        state="DocumentAvailable",
    )

    payload = ProcessResponse.from_decision(decision).model_dump(by_alias=True)

    assert payload["State"] == "DocumentAvailable"
    assert payload["StructuredState"] == {"agreement_id": "agr-1"}
    assert "ActiveContractId" not in payload
    assert "JourneyMilestone" not in payload
