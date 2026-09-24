from prompts import AGENT_INSTRUCTIONS


def test_named_websites_are_opened_directly() -> None:
    assert "If the user names a website, service, or domain" in AGENT_INSTRUCTIONS
    assert (
        "Do not unnecessarily route a request through a general search engine"
        in AGENT_INSTRUCTIONS
    )


def test_assistant_identity_is_sureedu() -> None:
    assert "You are Sureedu" in AGENT_INSTRUCTIONS
    assert "Jarvis" not in AGENT_INSTRUCTIONS


def test_tools_are_not_used_for_questions_about_itself() -> None:
    assert "without using any tools" in AGENT_INSTRUCTIONS
    assert "Never invent a justification" in AGENT_INSTRUCTIONS


def test_accent_is_consistent() -> None:
    assert "consistent British English accent" in AGENT_INSTRUCTIONS


def test_tab_rules_are_present() -> None:
    assert "# Browser Tabs" in AGENT_INSTRUCTIONS
    assert "Do not reopen a site that is already open in a tab" in AGENT_INSTRUCTIONS


def test_conversation_is_natural_not_scripted() -> None:
    assert "Do not end replies with a question or an offer" in AGENT_INSTRUCTIONS
    assert "Never guess an action" in AGENT_INSTRUCTIONS


def test_send_confirmation_names_recipient_and_text() -> None:
    assert "say who it goes to and the exact text" in AGENT_INSTRUCTIONS
    assert "One approval covers one action" in AGENT_INSTRUCTIONS
