from prompts import AGENT_INSTRUCTIONS


def test_named_websites_are_opened_directly() -> None:
    assert "If the user names a website, service, or domain" in AGENT_INSTRUCTIONS
    assert (
        "Do not unnecessarily route a request through a general search engine"
        in AGENT_INSTRUCTIONS
    )


def test_assistant_identity_is_yaadhamma() -> None:
    assert "You are Yaadhamma" in AGENT_INSTRUCTIONS
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


def test_window_versus_tab_is_explained() -> None:
    assert (
        '"Close the window" or "close the browser" means close_browser'
        in AGENT_INSTRUCTIONS
    )
    assert 'Never say "Anything else I can help with?"' in AGENT_INSTRUCTIONS


def test_side_conversations_and_invented_errors() -> None:
    assert (
        "stay silent and do nothing until the user addresses you again"
        in AGENT_INSTRUCTIONS
    )
    assert (
        "Only report a problem, such as a missing permission, when a tool actually returned it"
        in AGENT_INSTRUCTIONS
    )


def test_greeting_is_brief() -> None:
    assert "Do not add an offer of help" in AGENT_INSTRUCTIONS


def test_approval_flow_is_described() -> None:
    assert "It performs the waiting action itself" in AGENT_INSTRUCTIONS
    assert "The system recognises their spoken yes" in AGENT_INSTRUCTIONS


def test_observation_tool_is_explained() -> None:
    assert "call observe_state rather than guessing" in AGENT_INSTRUCTIONS
    assert "You cannot see images yet" in AGENT_INSTRUCTIONS


def test_voice_prompt_is_short() -> None:
    from prompts import VOICE_INSTRUCTIONS

    words = len(VOICE_INSTRUCTIONS.split())
    assert words < len(AGENT_INSTRUCTIONS.split()) / 4
    assert "run_task" in VOICE_INSTRUCTIONS
    assert "continue_task" in VOICE_INSTRUCTIONS
    assert "At your service, Sir" in VOICE_INSTRUCTIONS
    assert "Babai" in VOICE_INSTRUCTIONS


def test_orchestrator_prompt_defines_the_question_protocol() -> None:
    from prompts import ORCHESTRATOR_INSTRUCTIONS

    assert 'starts with "QUESTION:"' in ORCHESTRATOR_INSTRUCTIONS
    assert "Never use CSS selectors" in ORCHESTRATOR_INSTRUCTIONS
    assert "Never rephrase or add to it" in ORCHESTRATOR_INSTRUCTIONS
