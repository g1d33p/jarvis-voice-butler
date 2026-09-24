from prompts import AGENT_INSTRUCTIONS


def test_named_websites_are_opened_directly() -> None:
    assert "If the user names a website, service, or domain" in AGENT_INSTRUCTIONS
    assert "Do not unnecessarily route a request through a general search engine" in AGENT_INSTRUCTIONS


def test_assistant_identity_is_sureedu() -> None:
    assert "You are Sureedu" in AGENT_INSTRUCTIONS
    assert "Jarvis" not in AGENT_INSTRUCTIONS
