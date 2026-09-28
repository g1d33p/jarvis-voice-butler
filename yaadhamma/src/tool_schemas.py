"""Pre-flight check: every voice tool must be describable to Gemini.

Gemini Live converts the agent's tool list into Gemini function
declarations at session startup. A tool whose type hints cannot be
resolved — e.g. a name imported only inside a class body, where
typing.get_type_hints cannot see it — crashes the whole voice session
with NameError before Jeevan says a word (2026-09-28: planner.py's
_RunContext).

collect_voice_tools() rebuilds the voice tool list exactly the way
src/agent.py registers it in split mode, with inert stand-ins: no
browser opens, no real clients are built, no network, no model calls.
build_gemini_schemas() runs the exact LiveKit -> Gemini conversion the
realtime session uses. check_voice_tool_schemas() ties them together so
the self-test, the scorecard, and pytest all catch an undescribable
tool before a live run.

Keep the toolset list in sync with src/agent.py.
"""

from __future__ import annotations

from pathlib import Path


def collect_voice_tools(data_dir: Path | None = None) -> list:
    """The voice tool list, exactly as the agent registers it in split mode.

    Toolsets are built with inert stand-ins (browser=None, no real mail /
    chat / calendar clients). Stores go under data_dir when given, else
    their default locations. Nothing is called.
    """
    from livekit.agents.beta.tools import EndCallTool

    from actions import ActionRegistry
    from audit import AuditLog
    from calendar_tools import CalendarTools
    from commitment_tools import CommitmentTools
    from digest import DigestStore, DigestTools
    from file_tools import FileTools
    from gmail_tools import GmailTools
    from mac_tools import MacTools
    from memory_store import MemoryStore
    from memory_tools import MemoryTools
    from observation import ObservationTools
    from orchestrator import Orchestrator, TaskTools, voice_tools
    from permissions import ApprovalManager, ApprovalTools
    from planner import PlanTools
    from task_manager import TaskStore
    from task_store import CommitmentStore
    from tools import BrowserTools
    from whatsapp import WhatsAppClient
    from whatsapp_tools import WhatsAppTools

    def _db(name: str) -> Path | None:
        return data_dir / name if data_dir is not None else None

    audit = AuditLog(path=_db("audit.jsonl"))
    approvals = ApprovalManager(audit)
    toolsets = (
        BrowserTools(browser=None, approvals=approvals),
        MacTools(approvals=approvals),
        FileTools(approvals=approvals),
        GmailTools(clients=[], approvals=approvals),
        ApprovalTools(approvals=approvals),
        ObservationTools(browser=None),
        MemoryTools(
            store=MemoryStore(path=_db("memory.db")),
            audit=audit,
        ),
        CommitmentTools(store=CommitmentStore(path=_db("commitments.db"))),
        WhatsAppTools(client=WhatsAppClient(browser=None), approvals=approvals),
        CalendarTools(approvals=approvals),
    )
    orchestrator = Orchestrator(
        registry=ActionRegistry(),
        store=TaskStore(path=_db("tasks.db")),
        client=None,  # never called; schemas only describe the tools
    )
    return [
        *voice_tools(*toolsets),
        *TaskTools(orchestrator).tools,
        *DigestTools(store=DigestStore(path=_db("digests.db"))).tools,
        *PlanTools().tools,
        *EndCallTool().tools,
    ]


def build_gemini_schemas(tools: list) -> list[dict]:
    """Run the exact LiveKit -> Gemini conversion the realtime session
    uses at startup. Raises on the first tool that cannot be described."""
    from livekit.agents.llm import ToolContext

    return ToolContext(tools).parse_function_tools(
        "google", use_parameters_json_schema=False
    )


def check_voice_tool_schemas(data_dir: Path | None = None) -> list[str]:
    """Build every voice tool's Gemini schema. Returns the tool names;
    raises the original error on the first undescribable tool."""
    tools = collect_voice_tools(data_dir)
    assert tools, "no voice tools collected"
    return [schema["name"] for schema in build_gemini_schemas(tools)]
