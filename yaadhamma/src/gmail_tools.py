"""Voice tools for the Gmail skill (multi-account mail via the Gmail API).

Gmail is Jeevan's primary daily inbox: three personal accounts whose
combined view mimics the "All accounts" inbox in his Outlook app on the
Mac. gmail_read_inbox and gmail_search_email therefore merge results
across every linked account, newest first.

Reading is low-risk and ungated. Sending an email is consequential: it
goes through the shared approval gate. A short email the user dictated
word for word may go without asking (the same policy as dictated chat
messages); anything Yaadhamma composed is read back first and needs a
clear yes.

Tool names are prefixed with gmail_ because the Outlook skill already
registers read_inbox / search_email / read_email / send_email.
"""

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from gmail import GmailAuthError, GmailClient, discover_labels
from permissions import (
    ApprovalManager,
    latest_user_message,
)
from policy import can_send_without_asking

_SIGNIN_HINT = (
    "No Gmail accounts are linked yet. On the Mac, run "
    "`uv run scripts/gmail_signin.py <label>` in the yaadhamma folder "
    "once per account (e.g. personal1, personal2), then try again."
)


class GmailTools:
    """Multi-account Gmail tools for the voice agent."""

    def __init__(
        self,
        clients: list[GmailClient] | None = None,
        approvals: ApprovalManager | None = None,
        token_dir=None,
    ) -> None:
        if clients is None:
            clients = [GmailClient(label=label) for label in discover_labels(token_dir)]
        self._clients = clients
        self._approvals = approvals or ApprovalManager()
        self._auto_sent_for: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.gmail_read_inbox,
            self.gmail_search_email,
            self.gmail_read_email,
            self.gmail_send_email,
        ]

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GmailAuthError as exc:
            raise ToolError(str(exc)) from exc

    def _require_clients(self) -> list[GmailClient]:
        if not self._clients:
            raise ToolError(_SIGNIN_HINT)
        return self._clients

    def _client_for_label(self, account_label: str) -> GmailClient:
        for client in self._clients:
            if client.label == account_label:
                return client
        raise ToolError(
            f"{account_label!r} is not a linked Gmail account. Linked: "
            + ", ".join(c.label for c in self._clients)
        )

    @staticmethod
    def _tag(client: GmailClient, message: dict) -> dict:
        tagged = dict(message)
        tagged["account"] = client.label
        # Composite id routes gmail_read_email back to the right account.
        tagged["id"] = f"{client.label}:{message.get('id')}"
        return tagged

    def _merge(self, per_account: list[tuple[GmailClient, list[dict]]], limit: int):
        messages: list[dict] = []
        for client, items in per_account:
            messages.extend(self._tag(client, m) for m in items)
        messages.sort(key=lambda m: m.get("internal_date", 0), reverse=True)
        return {"messages": messages[:limit], "account_errors": []}

    @function_tool()
    async def gmail_read_inbox(
        self, context: RunContext, limit: int = 5
    ) -> dict[str, object]:
        """List the newest emails across ALL linked Gmail accounts, newest first.

        This is Jeevan's primary daily inbox (his "All accounts" view).
        Returns short summaries with ids; use gmail_read_email for the
        full text of one.
        """
        clients = self._require_clients()
        per_account = []
        errors = []
        for client in clients:
            try:
                per_account.append((client, client.list_messages(limit=limit)))
            except GmailAuthError as exc:
                errors.append(f"{client.label}: {exc}")
        result = self._merge(per_account, limit)
        result["account_errors"] = errors
        return result

    @function_tool()
    async def gmail_search_email(
        self, context: RunContext, query: str, limit: int = 5
    ) -> dict[str, object]:
        """Search ALL linked Gmail accounts for emails about something.

        Args:
            query: What to look for, e.g. "electric bill" or "from:ravi".
        """
        clients = self._require_clients()
        per_account = []
        errors = []
        for client in clients:
            try:
                per_account.append((client, client.search_mail(query, limit=limit)))
            except GmailAuthError as exc:
                errors.append(f"{client.label}: {exc}")
        result = self._merge(per_account, limit)
        result["account_errors"] = errors
        return result

    @function_tool()
    async def gmail_read_email(
        self, context: RunContext, message_id: str
    ) -> dict[str, object]:
        """Read the full text of one Gmail message.

        Args:
            message_id: The id from a gmail_read_inbox or
                gmail_search_email result (looks like "personal1:abc123").
        """
        clients = self._require_clients()
        label, _, raw_id = message_id.partition(":")
        if raw_id:
            client = self._client_for_label(label)
            message = self._call(client.get_message, raw_id)
            return self._tag(client, message)
        # Bare id: try each account in turn.
        last_error = None
        for client in clients:
            try:
                return self._tag(client, client.get_message(message_id))
            except GmailAuthError as exc:
                last_error = exc
        raise ToolError(
            f"Could not find that message in any linked account: {last_error}"
        )

    @function_tool()
    async def gmail_send_email(
        self,
        context: RunContext,
        to: str,
        subject: str,
        body: str,
        account_label: str | None = None,
    ) -> dict[str, object]:
        """Send an email from a Gmail account.

        The draft (to, subject, body) is always shown to the user first
        unless they dictated it word for word. A clear yes sends it.
        Never invent the recipient: if the user names a person, resolve
        the address from memory or ask — do not guess an email address.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text body, exactly as it should be sent.
            account_label: Which linked Gmail account to send from
                (default: the first linked account).
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
        clients = self._require_clients()
        client = self._client_for_label(account_label) if account_label else clients[0]
        draft = f"To: {to}\nSubject: {subject}\n\n{body}"
        description = (
            f"send email from the {client.label!r} Gmail account "
            f"to {to} with subject {subject!r}"
        )

        async def execute() -> dict[str, object]:
            return self._call(client.send_mail, to, subject, body)

        return await self._approvals.gate(
            tool_name="gmail_send_email",
            description=description,
            context=context,
            execute=execute,
            args={"to": to, "subject": subject, "account_label": client.label},
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
