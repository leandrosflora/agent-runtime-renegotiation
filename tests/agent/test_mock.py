from app.agent.mock import HUMAN_HANDOFF_REASON, build_mock_decision


def test_human_keyword_forces_handoff_without_reply():
    decision = build_mock_decision("quero falar com um atendente", None, None)

    assert decision.requires_handoff is True
    assert decision.handoff_reason == HUMAN_HANDOFF_REASON
    assert decision.reply_text is None


def test_renegotiation_keyword_returns_renegotiation_intent():
    decision = build_mock_decision("quero renegociar minha divida", None, None)

    assert decision.intent == "renegotiation_request"
    assert decision.requires_handoff is False
    assert decision.reply_text


def test_debt_keyword_returns_debt_inquiry_intent():
    decision = build_mock_decision("quanto eu devo de boleto atrasado?", None, None)

    assert decision.intent == "debt_inquiry"
    assert decision.requires_handoff is False


def test_unmatched_text_falls_back_to_greeting():
    decision = build_mock_decision("oi, bom dia", None, None)

    assert decision.intent == "greeting"
    assert decision.requires_handoff is False


def test_empty_text_falls_back_to_greeting():
    decision = build_mock_decision(None, None, None)

    assert decision.intent == "greeting"
