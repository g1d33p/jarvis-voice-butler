"""Risk tiers and the unified approval gate.

The language model proposes; this code decides. Every consequential tool
goes through ApprovalManager.gate():

- LOW: read-only or trivially reversible. Runs immediately, audit-logged.
- MEDIUM: changes state but is recoverable (trash, quit an app, close a tab).
  Needs approval, but a clear yes already spoken in this conversation for this
  exact action counts.
- HIGH: irreversible or high-stakes (send a message, buy, submit, delete).
  Needs approval for that exact action: a short dictated message (see
  policy.can_send_without_asking), a clear yes already spoken in this
  conversation to the exact proposal, or a fresh ask-and-confirm round-trip.
  One approval covers one action, and sends are re-verified unchanged before
  they go out.

All decisions and outcomes are written to the audit log.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from audit import AuditLog

# How long a pending approval waits for the user's answer.
APPROVAL_TTL_SECONDS = 60

# How long a spoken yes to a proposed draft stays usable for MEDIUM actions.
SPOKEN_DRAFT_APPROVAL_SECONDS = 300


class RiskTier(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Static tiers per tool id. Unknown tools default to MEDIUM (fail closed).
# Enforcement note: TOOL_TIERS classifies risk for the audit log and the
# gate. Only some tools are routed through ApprovalManager.gate: risky clicks
# and consequential Enter presses (HIGH), quit_application and move_to_trash
# (MEDIUM). close_tab/close_browser (MEDIUM) keep their dedicated unsaved-text
# confirmation flow. File create/rename/move/copy stay ungated on purpose: a
# direct "create X" instruction is itself the approval, and asking again would
# punish the common case; unprompted file writes are forbidden by the prompt.
TOOL_TIERS: dict[str, RiskTier] = {
    # Browser (tools.BrowserTools)
    "search_the_web": RiskTier.LOW,
    "open_url": RiskTier.LOW,
    "read_page": RiskTier.LOW,
    "inspect_page": RiskTier.LOW,
    "go_back": RiskTier.LOW,
    "take_screenshot": RiskTier.LOW,
    "scroll": RiskTier.LOW,
    "click": RiskTier.LOW,  # dynamic: risky targets are HIGH
    "press_key": RiskTier.LOW,  # dynamic: consequential Enter is HIGH
    "type_text": RiskTier.LOW,
    "list_tabs": RiskTier.LOW,
    "switch_tab": RiskTier.LOW,
    "open_tab": RiskTier.LOW,
    "close_tab": RiskTier.MEDIUM,
    "close_browser": RiskTier.MEDIUM,
    "reload_page": RiskTier.LOW,
    # Mac (mac_tools.MacTools)
    "list_running_apps": RiskTier.LOW,
    "open_application": RiskTier.LOW,
    "quit_application": RiskTier.MEDIUM,
    "read_clipboard": RiskTier.LOW,
    "write_clipboard": RiskTier.LOW,
    "capture_screen": RiskTier.LOW,
    # Files (file_tools.FileTools)
    "get_home_directory": RiskTier.LOW,
    "list_directory": RiskTier.LOW,
    "search_files": RiskTier.LOW,
    "inspect_path": RiskTier.LOW,
    "create_folder": RiskTier.MEDIUM,
    "create_file": RiskTier.MEDIUM,
    "rename_path": RiskTier.MEDIUM,
    "move_path": RiskTier.MEDIUM,
    "copy_path": RiskTier.MEDIUM,
    "move_to_trash": RiskTier.MEDIUM,
    "open_path": RiskTier.LOW,
    # Background tasks (orchestrator.TaskTools)
    "run_task": RiskTier.LOW,
    "continue_task": RiskTier.LOW,
    "recent_tasks": RiskTier.LOW,
    # The approval tool itself
    "approve_pending_action": RiskTier.LOW,
}

# Words in a clicked control's name that make the click HIGH risk.
RISKY_CLICK_WORDS = {
    "buy",
    "confirm",
    "delete",
    "order",
    "pay",
    "purchase",
    "remove",
    "send",
    "submit",
}


def is_risky_click_target(text: str) -> bool:
    """True if clicking something named `text` is a HIGH-risk action."""
    return bool(RISKY_CLICK_WORDS.intersection(text.casefold().split()))


def tier_for(tool_name: str, args: dict[str, Any] | None = None) -> RiskTier:
    """Resolve the risk tier of one tool call."""
    args = args or {}
    if tool_name == "click" and is_risky_click_target(
        f"{args.get('target', '')} {args.get('label', '')}"
    ):
        return RiskTier.HIGH
    if tool_name == "press_key" and args.get("key") == "Enter":
        if args.get("consequential"):
            return RiskTier.HIGH
        return RiskTier.LOW
    return TOOL_TIERS.get(tool_name, RiskTier.MEDIUM)


_AFFIRMATIVE = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "proceed",
    "absolutely",
    "definitely",
    "correct",
    "affirmative",
    # Telugu / Hindi
    "avunu",
    "sare",
    "sari",
    "haan",
    "cheyyi",
    "pampu",
}
_AFFIRMATIVE_PHRASES = (
    "go ahead",
    "do it",
    "send it",
    "please do",
    "go for it",
    "that works",
    "sounds good",
    "looks good",
)
# While a message is waiting to be sent, a plain instruction to send it
# ("send the message", "what are you waiting for, send it") is also a yes.
_SEND_COMMANDS = {"send", "pampu", "bhejo"}
_NEGATIVE = {
    "no",
    "nope",
    "not",
    "don't",
    "dont",
    "wait",
    "stop",
    "cancel",
    "hold",
    "never",
    "vaddu",
    "ledu",
    "nahi",
}


def is_clear_approval(reply: str, *, sending: bool = False) -> bool:
    """True only for an unambiguous yes; anything unclear counts as no.

    With sending=True, a direct instruction to send also counts as a yes.
    """
    words = re.findall(r"[a-z']+", reply.casefold())
    if not words or _NEGATIVE.intersection(words):
        return False
    text = " ".join(words)
    if sending and _SEND_COMMANDS.intersection(words):
        return True
    return bool(_AFFIRMATIVE.intersection(words)) or any(
        phrase in text for phrase in _AFFIRMATIVE_PHRASES
    )


def normalize_message(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".!").casefold()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.casefold())


def _names_action(keywords: set[str], text_words: list[str]) -> bool:
    """Did the question name the action? Tolerates inflections.

    "send" matches "sending"; plain substring matching was too loose ("hi"
    matched inside "this") and exact word matching was too strict ("send"
    missed "sending"). A shared prefix of at least 3 characters in either
    direction covers the common inflections. When unsure we return False:
    missing an approval just means asking once more, while a false match
    would act without approval.
    """
    for keyword in keywords:
        for word in text_words:
            if keyword == word:
                return True
            if len(keyword) >= 3 and word.startswith(keyword):
                return True
            if len(word) >= 3 and keyword.startswith(word):
                return True
    return False


def latest_user_message(context: object) -> tuple[str, str] | None:
    """Return (id, text) of the most recent thing the user actually said."""
    try:
        for item in reversed(context.session.history.items):  # type: ignore[attr-defined]
            if getattr(item, "type", "") == "message" and item.role == "user":
                return str(getattr(item, "id", "")), item.text_content or ""
    except Exception:
        return None
    return None


def _latest_user_text(context: object) -> str | None:
    latest = latest_user_message(context)
    return latest[1] if latest else None


def _messages(context: object) -> list:
    """The conversation so far as chat messages, oldest first (empty if unknown)."""
    try:
        return [
            item
            for item in context.session.history.items  # type: ignore[attr-defined]
            if getattr(item, "type", "") == "message"
        ]
    except Exception:
        return []


# Text in quotation marks, e.g. 'hi' or "hi" or \u201chi\u201d. The opening quote
# must start a word, so apostrophes as in "it's" or "I'll" are not quotes.
_QUOTED = re.compile(
    '(?:^|[\\s:(])["\'\u201c\u2018]([^"\u201d\u2019]{3,}?)["\'\u201d\u2019](?=[\\s.,!?)]|$)'
)

_DESCRIPTION_STOPWORDS = {"to", "the", "a", "an", "it", "this", "that", "whether"}


def spoken_approval(
    context: object, description: str, *, quoted: str = ""
) -> tuple[bool, str | None]:
    """Did the user already approve this exact action in conversation?

    `description` is the human phrasing of the action ("send 'hi' to Ravi",
    "quit Google Chrome"). `quoted` is exact text the assistant proposed
    (for example a message draft) that the description refers to.

    Two cases count, both judged on the real transcript:
    - Yaadhamma's latest question named the action (for example "Shall I send
      it?") and the user's reply right after it is a clear yes.
    - Yaadhamma proposed the exact text, and the user's very next reply was a
      clear yes (for example "That works"), within five minutes.
    Returns (approved, id of the approving user message).
    """
    messages = _messages(context)
    wanted = normalize_message(quoted or description)
    keywords = {w for w in _words(description) if w not in _DESCRIPTION_STOPWORDS}
    sending = "send" in description.casefold()

    for index in range(len(messages) - 1, 0, -1):
        reply, question = messages[index], messages[index - 1]
        if reply.role != "user" or question.role != "assistant":
            continue
        said = reply.text_content or ""
        asked = (question.text_content or "").casefold()
        if not is_clear_approval(said, sending=sending):
            continue
        is_latest_reply = all(m.role != "user" for m in messages[index + 1 :])
        quotes = [normalize_message(q) for q in _QUOTED.findall(asked)]
        # If the question quoted a message, it must be this one.
        matches_quote = not quotes or any(wanted and wanted in q for q in quotes)
        if (
            is_latest_reply
            and "?" in asked
            and keywords
            and _names_action(keywords, _words(asked))
            and matches_quote
        ):
            return True, str(reply.id)
        quoted_match = bool(quoted) and wanted and wanted in normalize_message(asked)
        fresh = time.time() - getattr(reply, "created_at", 0) <= (
            SPOKEN_DRAFT_APPROVAL_SECONDS
        )
        if quoted_match and fresh:
            return True, str(reply.id)
    return False, None


@dataclass
class PendingApproval:
    """A consequential action stopped to ask the user first."""

    id: str
    tool_name: str
    tier: RiskTier
    description: str  # human phrasing, e.g. "send 'hi' to Ravi"
    execute: Callable[[], Awaitable[Any]]  # performs the action
    verify: Callable[[], Awaitable[None]] | None = None  # re-check before running
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float = field(
        default_factory=lambda: time.monotonic() + APPROVAL_TTL_SECONDS
    )


class ApprovalManager:
    """One place for every approval decision Yaadhamma makes.

    The voice agent and the background orchestrator share one manager (and one
    browser, one audit log), so a pending approval can be answered from either
    path. The model proposes; this code decides.
    """

    def __init__(
        self, audit: AuditLog | None = None, ttl_seconds: float = APPROVAL_TTL_SECONDS
    ) -> None:
        self._audit = audit or AuditLog()
        self._ttl_seconds = ttl_seconds
        self._pending: PendingApproval | None = None
        self._used_approvals: set[str] = set()

    @property
    def pending(self) -> PendingApproval | None:
        return self._pending

    def cancel_pending(self) -> None:
        """Drop anything waiting: the context it referred to changed."""
        if self._pending is not None:
            self._audit.record(
                "approval_cancelled",
                tool=self._pending.tool_name,
                description=self._pending.description,
            )
        self._pending = None

    async def gate(
        self,
        *,
        tool_name: str,
        description: str,
        context: object,
        execute: Callable[[], Awaitable[Any]],
        args: dict[str, Any] | None = None,
        tier: RiskTier | None = None,
        verify: Callable[[], Awaitable[None]] | None = None,
        pre_approved: str | None = None,
        quoted: str = "",
    ) -> Any:
        """Run `execute` if allowed; otherwise stash it and ask the user.

        `pre_approved` carries the reason when a policy already approved this
        call (for example the dictated-message rule). `quoted` is exact text
        the user was shown (for example a message draft) that a spoken yes may
        have approved. Raises ToolError to stop the tool and tell the model to
        ask the user.
        """
        tier = tier or tier_for(tool_name, args)
        args = args or {}
        self._audit.record(
            "tool_call",
            tool=tool_name,
            tier=tier.value,
            description=description,
            args=args,
        )

        if tier is RiskTier.LOW:
            return await self._run(tool_name, description, execute, basis="low-risk")

        if pre_approved:
            return await self._run(
                tool_name, description, execute, basis=f"policy: {pre_approved}"
            )

        # A clear yes already spoken in this conversation to the exact action
        # counts (for example the user approved a proposed message draft).
        approved, approval_id = spoken_approval(context, description, quoted=quoted)
        if approved and approval_id and approval_id not in self._used_approvals:
            self._used_approvals.add(approval_id)
            return await self._run(
                tool_name, description, execute, basis="spoken-approval"
            )

        # HIGH, or MEDIUM with no usable prior yes: stop and ask.
        self._pending = PendingApproval(
            id=uuid.uuid4().hex[:12],
            tool_name=tool_name,
            tier=tier,
            description=description,
            execute=execute,
            verify=verify,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        self._audit.record(
            "approval_requested",
            tool=tool_name,
            tier=tier.value,
            description=description,
        )
        raise ToolError(
            "Not done yet: this needs the user's approval. Ask once, naturally, "
            f"whether to {description}. When they agree, call "
            "approve_pending_action with their exact reply; it performs the "
            "action itself, so do not do it again yourself. If they say no or "
            "the reply is unclear, drop it."
        )

    async def confirm(self, context: object, user_reply: str) -> Any:
        """Carry out the waiting action, now that the user answered."""
        pending = self._pending
        if pending is None or time.monotonic() > pending.expires_at:
            self._pending = None
            self._audit.record("approval_expired")
            raise ToolError(
                "Nothing is waiting for approval any more. Try the action again; "
                "it will say whether approval is needed."
            )
        heard = _latest_user_text(context) or user_reply
        if not is_clear_approval(heard, sending="send" in pending.description):
            if _NEGATIVE.intersection(_words(heard)):
                self._pending = None
                self._audit.record(
                    "approval_denied",
                    tool=pending.tool_name,
                    description=pending.description,
                )
                raise ToolError("The user said no. Nothing was done; drop it.")
            # Unclear (noise, echo, "very well"): keep it waiting and ask again.
            raise ToolError(
                "That reply is not a clear yes, so nothing was done yet. The action "
                "is still waiting: ask again briefly, then call "
                "approve_pending_action again."
            )
        self._pending = None
        if pending.verify is not None:
            await pending.verify()
        self._audit.record(
            "approval_confirmed",
            tool=pending.tool_name,
            tier=pending.tier.value,
            description=pending.description,
        )
        return await self._run(
            pending.tool_name,
            pending.description,
            pending.execute,
            basis="explicit-approval",
        )

    async def _run(
        self,
        tool_name: str,
        description: str,
        execute: Callable[[], Awaitable[Any]],
        *,
        basis: str,
    ) -> Any:
        try:
            result = await execute()
        except Exception as exc:
            self._audit.record(
                "action_failed",
                tool=tool_name,
                description=description,
                basis=basis,
                error=str(exc)[:200],
            )
            raise
        self._audit.record(
            "action_done",
            tool=tool_name,
            description=description,
            basis=basis,
            result=result if isinstance(result, (str, int, float, bool)) else "...",
        )
        return result


class ApprovalTools:
    """The one tool the model uses to complete an approval round-trip."""

    def __init__(self, approvals: ApprovalManager | None = None) -> None:
        self._approvals = approvals or ApprovalManager()

    @property
    def tools(self) -> list:
        return [self.approve_pending_action]

    @function_tool()
    async def approve_pending_action(
        self, context: RunContext, user_reply: str
    ) -> dict[str, Any]:
        """Carry out the action that was stopped for approval, now that the user agreed.

        Call this right after the user answers your confirmation question, with
        their exact reply. It performs the waiting action itself, so do not do
        it again afterwards. Only report success if this returns without an error.

        Args:
            user_reply: The user's exact words in reply to your question.
        """
        return await self._approvals.confirm(context, user_reply)
