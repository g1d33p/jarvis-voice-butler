"""Voice tools for the WhatsApp skill (WhatsApp Web in Yaadhamma's browser).

Triage is the point: whatsapp_list_chats and whatsapp_where_needed gather
what the LLM needs to tell Jeevan where he is needed — unread chats with
their recent messages — and the LLM does the understanding.

Reading is low-risk and ungated. Sending a message is consequential: it goes
through the shared approval gate. A short message the user dictated word for
word may go without asking (the same policy as dictated emails); anything
Yaadhamma composed is read back first and needs a clear yes.

Names are never guessed: an unknown or ambiguous chat name stops with a
question instead of picking one.

Note: opening a chat marks its messages as read in WhatsApp, exactly as if
Jeevan had opened it himself. Triage therefore consumes unread state.
"""

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from permissions import (
    ApprovalManager,
    latest_user_message,
)
from policy import can_send_without_asking
from whatsapp import WhatsAppClient, WhatsAppError, WhatsAppNotPairedError

_SIGNIN_HINT = (
    "WhatsApp is not paired yet. On the Mac, run "
    "`uv run scripts/whatsapp_signin.py` in the yaadhamma folder and scan "
    "the QR code with the phone, then try again."
)


class WhatsAppTools:
    """WhatsApp triage and messaging tools for the voice agent."""

    def __init__(
        self,
        client: WhatsAppClient | None = None,
        browser=None,
        approvals: ApprovalManager | None = None,
    ) -> None:
        self._client = client or WhatsAppClient(browser=browser)
        self._approvals = approvals or ApprovalManager()
        self._auto_sent_for: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.whatsapp_list_chats,
            self.whatsapp_read_chat,
            self.whatsapp_where_needed,
            self.whatsapp_send_message,
        ]

    async def _guarded(self, fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except WhatsAppNotPairedError as exc:
            raise ToolError(_SIGNIN_HINT) from exc
        except WhatsAppError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def whatsapp_list_chats(
        self, context: RunContext, limit: int = 30
    ) -> dict[str, object]:
        """List WhatsApp chats with unread counts and last-message previews.

        Use for a quick overview of what's waiting. For "where am I needed",
        prefer whatsapp_where_needed.
        """
        chats = await self._guarded(self._client.list_all_chats)
        return {"chats": chats[:limit], "total": len(chats)}

    @function_tool()
    async def whatsapp_read_chat(
        self, context: RunContext, chat_name: str, limit: int = 10
    ) -> dict[str, object]:
        """Read recent messages from one WhatsApp chat, oldest first.

        Opening the chat marks its messages as read in WhatsApp.
        If the name is unknown or matches several chats, this stops and
        asks instead of guessing.

        Args:
            chat_name: Contact or group name, e.g. "Ravi" or "Family Group".
            limit: How many recent messages to read (default 10).
        """
        chat_name = (chat_name or "").strip()
        if not chat_name:
            raise ToolError("Which chat should I read? Give me a name.")
        return await self._guarded(self._client.read_messages, chat_name, limit)

    @function_tool()
    async def whatsapp_where_needed(
        self,
        context: RunContext,
        max_chats: int = 8,
        messages_per_chat: int = 6,
    ) -> dict[str, object]:
        """Triage: gather the WhatsApp chats where Jeevan is needed.

        Returns every chat with unread messages plus its recent messages, so
        you can summarize what needs his attention: questions for him,
        @mentions, time-sensitive asks. Opening a chat marks it as read.

        Args:
            max_chats: Maximum unread chats to open (default 8).
            messages_per_chat: Recent messages to include per chat (default 6).
        """
        chats = await self._guarded(self._client.list_all_chats)
        unread = [c for c in chats if c.get("unread", 0) > 0]
        attention = []
        for chat in unread[:max_chats]:
            messages = await self._guarded(
                self._client.read_messages, chat["name"], messages_per_chat
            )
            attention.append(
                {
                    "chat": chat["name"],
                    "unread": chat.get("unread", 0),
                    "preview": chat.get("preview", ""),
                    "messages": messages["messages"],
                }
            )
        return {
            "chats_needing_attention": attention,
            "unread_chat_count": len(unread),
            "total_chats": len(chats),
        }

    @function_tool()
    async def whatsapp_send_message(
        self, context: RunContext, chat_name: str, message: str
    ) -> dict[str, object]:
        """Send a WhatsApp message to a contact or group.

        The message is always shown to the user first unless they dictated
        it word for word. A clear yes sends it. Never invent the chat: an
        unknown or ambiguous name stops and asks instead of guessing.

        Args:
            chat_name: Contact or group name, e.g. "Ravi".
            message: The message text, exactly as it should be sent.
        """
        chat_name, message = (chat_name or "").strip(), (message or "").strip()
        if not chat_name or not message:
            raise ToolError("To send a WhatsApp message I need a chat and a message.")
        # Resolve the chat BEFORE the approval gate, so a yes can never
        # approve a guessed recipient.
        matched = await self._guarded(self._client.find_chat, chat_name)
        description = f"send WhatsApp message to {matched}: {message!r}"
        quoted = f"To {matched} on WhatsApp:\n\n{message}"

        async def execute() -> dict[str, object]:
            return await self._guarded(self._client.send_message, matched, message)

        return await self._approvals.gate(
            tool_name="whatsapp_send_message",
            description=description,
            context=context,
            execute=execute,
            args={"chat": matched},
            pre_approved=self._dictated_message_ok(context, message),
            quoted=quoted,
        )

    def _dictated_message_ok(self, context: object, message: str) -> str | None:
        """Policy reason if the user dictated this exact message, else None."""
        latest = latest_user_message(context)
        if latest is None:
            return None
        utterance_id, user_words = latest
        allowed, reason = can_send_without_asking(message, user_words)
        key = utterance_id or user_words
        if not allowed or key == self._auto_sent_for:
            return None
        self._auto_sent_for = key
        return reason
