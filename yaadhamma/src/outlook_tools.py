"""Voice tools for the Outlook skill (mail + calendar via Microsoft Graph).

Reading is low-risk and ungated. Sending an email is consequential: it goes
through the shared approval gate. A short email the user dictated word for
word may go without asking (the same policy as dictated chat messages);
anything Yaadhamma composed is read back first and needs a clear yes.
"""

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from outlook import OutlookAuthError, OutlookClient
from permissions import (
    ApprovalManager,
    latest_user_message,
)
from policy import can_send_without_asking


class OutlookTools:
    """Mail and calendar tools for the voice agent."""

    def __init__(
        self,
        client: OutlookClient | None = None,
        approvals: ApprovalManager | None = None,
    ) -> None:
        self._client = client or OutlookClient()
        self._approvals = approvals or ApprovalManager()
        self._auto_sent_for: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.read_inbox,
            self.search_email,
            self.read_email,
            self.check_calendar,
            self.send_email,
        ]

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OutlookAuthError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def read_inbox(
        self, context: RunContext, limit: int = 5
    ) -> dict[str, object]:
        """List the most recent emails in the inbox, newest first.

        Use when the user asks what's new in email or whether something
        arrived. Returns short summaries with ids; use read_email for the
        full text of one.
        """
        messages = self._call(self._client.list_inbox, limit=limit)
        return {"messages": messages}

    @function_tool()
    async def search_email(
        self, context: RunContext, query: str, limit: int = 5
    ) -> dict[str, object]:
        """Search the mailbox for emails about something.

        Args:
            query: What to look for, e.g. "electric bill" or "Ravi".
        """
        messages = self._call(self._client.search_mail, query, limit=limit)
        return {"messages": messages}

    @function_tool()
    async def read_email(
        self, context: RunContext, message_id: str
    ) -> dict[str, object]:
        """Read the full text of one email.

        Args:
            message_id: The id from a read_inbox or search_email result.
        """
        return self._call(self._client.get_message, message_id)

    @function_tool()
    async def check_calendar(
        self, context: RunContext, days: int = 1
    ) -> dict[str, object]:
        """List upcoming calendar events, soonest first.

        Args:
            days: How many days ahead to look (default 1, today).
        """
        events = self._call(self._client.list_calendar, days=days)
        return {"events": events}

    @function_tool()
    async def send_email(
        self, context: RunContext, to: str, subject: str, body: str
    ) -> dict[str, object]:
        """Send an email via Outlook.

        The draft (to, subject, body) is always shown to the user first
        unless they dictated it word for word. A clear yes sends it.
        Never invent the recipient: if the user names a person, resolve
        the address from memory or ask — do not guess an email address.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text body, exactly as it should be sent.
        """
        to, subject, body = to.strip(), subject.strip(), body.strip()
        if not to or not subject or not body:
            raise ToolError(
                "To send an email I need a recipient, a subject, and a body."
            )
        if "@" not in to:
            raise ToolError(
                f"{to!r} does not look like an email address. Ask the user "
                "for the address instead of guessing."
            )
        draft = f"To: {to}\nSubject: {subject}\n\n{body}"
        description = f"send email to {to} with subject {subject!r}"

        async def execute() -> dict[str, object]:
            return self._call(self._client.send_mail, to, subject, body)

        return await self._approvals.gate(
            tool_name="send_email",
            description=description,
            context=context,
            execute=execute,
            args={"to": to, "subject": subject},
            pre_approved=self._dictated_email_ok(context, body),
            quoted=draft,
        )

    def _dictated_email_ok(self, context: object, body: str) -> str | None:
        """Policy reason if the user dictated this exact email, else None."""
        latest = latest_user_message(context)
        if latest is None:
            return None
        utterance_id, user_words = latest
        allowed, reason = can_send_without_asking(body, user_words)
        key = utterance_id or user_words
        if not allowed or key == self._auto_sent_for:
            return None
        self._auto_sent_for = key
        return reason
