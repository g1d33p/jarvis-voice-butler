"""Fake backends for the task scorecard.

Each fake mimics the real client's interface closely enough that the REAL
tool classes (GmailTools, WhatsAppTools, ...) run against it. Faults are
injected with fail_next(exc): the next call of any method raises it and the
fake counts it in faults_fired, which the scorecard runner turns into the
recovery signal. Nothing here touches the network.
"""

from __future__ import annotations

from whatsapp import WhatsAppError, WhatsAppNotPairedError


class FaultMixin:
    """fail_next(exc) makes the next backend call raise; counted for recovery."""

    def __init__(self) -> None:
        self.faults_fired = 0
        self._fault_queue: list[BaseException] = []

    def fail_next(self, exc: BaseException) -> None:
        self._fault_queue.append(exc)

    def _maybe_fault(self) -> None:
        if self._fault_queue:
            self.faults_fired += 1
            raise self._fault_queue.pop(0)


def sim_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Simulated cost for an operation, priced with the real rate table."""
    from costs import PRICE_PER_1K

    per_in, per_out = PRICE_PER_1K.get(model, (0.0, 0.0))
    return tokens_in / 1000 * per_in + tokens_out / 1000 * per_out


# ---------------------------------------------------------------- Gmail


class FakeGmailClient(FaultMixin):
    """Pretends to be gmail.GmailClient for one account label."""

    def __init__(self, label: str = "personal", messages=(), verify_sends: bool = True):
        super().__init__()
        self.label = label
        self._messages = list(messages)
        self._verify_sends = verify_sends
        self.sent: list[dict] = []

    def _summary(self, msg: dict) -> dict:
        return {
            "id": msg["id"],
            "subject": msg["subject"],
            "from": msg["from"],
            "date": msg.get("date", ""),
            "internal_date": msg.get("internal_date", 0),
            "snippet": msg.get("snippet", ""),
            "is_read": msg.get("is_read", True),
        }

    def list_messages(self, limit: int = 10) -> list[dict]:
        self._maybe_fault()
        ordered = sorted(
            self._messages, key=lambda m: m.get("internal_date", 0), reverse=True
        )
        return [self._summary(m) for m in ordered[:limit]]

    def search_mail(self, query: str, limit: int = 10) -> list[dict]:
        self._maybe_fault()
        q = query.casefold()
        hits = [
            m
            for m in self._messages
            if q in m["subject"].casefold()
            or q in m["from"].casefold()
            or q in m.get("snippet", "").casefold()
        ]
        ordered = sorted(hits, key=lambda m: m.get("internal_date", 0), reverse=True)
        return [self._summary(m) for m in ordered[:limit]]

    def get_message(self, message_id: str) -> dict:
        self._maybe_fault()
        for msg in self._messages:
            if msg["id"] == message_id:
                return {
                    "from": msg["from"],
                    "subject": msg["subject"],
                    "snippet": msg.get("snippet", ""),
                    "body_text": msg.get("body_text", msg.get("snippet", "")),
                }
        raise ValueError(f"no such message: {message_id}")

    def send_mail(self, to: str, subject: str, body: str) -> dict:
        self._maybe_fault()
        message_id = f"sent-{len(self.sent) + 1}"
        self.sent.append({"to": to, "subject": subject, "body": body, "id": message_id})
        if self._verify_sends:
            return {
                "sent": True,
                "to": to,
                "id": message_id,
                "verified": True,
                "verification": f"message {message_id} re-fetched with SENT label",
            }
        return {
            "sent": True,
            "to": to,
            "id": message_id,
            "verified": False,
            "verification": "re-fetch failed; the send is unconfirmed",
        }


def gmail_message(
    msg_id: str,
    sender: str,
    subject: str,
    snippet: str,
    internal_date: int,
    is_read: bool = False,
    body_text: str = "",
) -> dict:
    return {
        "id": msg_id,
        "from": sender,
        "subject": subject,
        "snippet": snippet,
        "body_text": body_text or snippet,
        "internal_date": internal_date,
        "is_read": is_read,
    }


# ---------------------------------------------------------------- WhatsApp


class FakeWhatsAppClient(FaultMixin):
    """Pretends to be whatsapp.WhatsAppClient (already-paired browser tab)."""

    def __init__(self, chats=(), paired: bool = True):
        super().__init__()
        self._chats = list(chats)
        self._paired = paired
        self.sent: list[dict] = []

    def _need_paired(self) -> None:
        if not self._paired:
            raise WhatsAppNotPairedError("not paired")

    async def list_all_chats(self, max_rounds: int = 6) -> list[dict]:
        self._maybe_fault()
        self._need_paired()
        return list(self._chats)

    async def find_chat(self, name: str, max_rounds: int = 6) -> str:
        self._maybe_fault()
        self._need_paired()
        target = name.strip().casefold()
        exact = [c for c in self._chats if c["name"].casefold() == target]
        if len(exact) == 1:
            return exact[0]["name"]
        partial = [c for c in self._chats if target and target in c["name"].casefold()]
        if not partial:
            raise WhatsAppError(f"No WhatsApp chat named {name!r} found.")
        if len(partial) > 1:
            names = ", ".join(c["name"] for c in partial)
            raise WhatsAppError(f"Several chats match {name!r}: {names}.")
        return partial[0]["name"]

    async def read_messages(
        self, chat_name: str, limit: int = 15, exact: bool = False
    ) -> dict:
        self._maybe_fault()
        self._need_paired()
        resolved = await self.find_chat(chat_name) if not exact else chat_name
        for chat in self._chats:
            if chat["name"] == resolved:
                return {
                    "chat": resolved,
                    "messages": list(chat.get("messages", []))[-limit:],
                }
        raise WhatsAppError(f"No WhatsApp chat named {chat_name!r} found.")

    async def send_message(self, chat: str, message: str) -> dict:
        self._maybe_fault()
        self._need_paired()
        self.sent.append({"chat": chat, "text": message})
        return {"sent": True, "chat": chat}


def wa_chat(name: str, unread: int = 0, preview: str = "", messages=()) -> dict:
    return {
        "name": name,
        "unread": unread,
        "preview": preview,
        "time": "10:30",
        "messages": list(messages),
    }


def wa_msg(sender: str, text: str) -> dict:
    return {"sender": sender, "text": text, "time": "10:31"}


# ---------------------------------------------------------------- Calendar


class FakeCalendarClient(FaultMixin):
    """Pretends to be gcal.CalendarClient."""

    def __init__(self, events=()):
        super().__init__()
        self._events = {e["id"]: dict(e) for e in events}
        self.created: list[dict] = []

    def list_events(self, start, end) -> list[dict]:
        self._maybe_fault()
        return [e for e in self._events.values() if start <= e["begin"] < end]

    def create_event(self, title, begin, finish, location="") -> dict:
        self._maybe_fault()
        event_id = f"evt-{len(self._events) + 1}"
        event = {
            "id": event_id,
            "title": title,
            "begin": begin,
            "start": begin.isoformat(),
            "end": finish.isoformat(),
            "location": location or "",
            "all_day": False,
            "busy": True,
            "declined": False,
        }
        self._events[event_id] = event
        self.created.append(event)
        return {"id": event_id}

    def get_event(self, event_id: str) -> dict:
        self._maybe_fault()
        if event_id not in self._events:
            raise ValueError(f"no such event: {event_id}")
        return dict(self._events[event_id])


def cal_event(
    event_id: str,
    title: str,
    start,
    minutes: int = 60,
    location: str = "",
    all_day: bool = False,
    busy: bool = True,
    declined: bool = False,
) -> dict:
    """A calendar event with every field the agenda/clash code reads."""
    from datetime import timedelta

    return {
        "id": event_id,
        "title": title,
        "begin": start,
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "location": location,
        "all_day": all_day,
        "busy": busy,
        "declined": declined,
    }


# ---------------------------------------------------------------- Context


class _Item:
    def __init__(self, role: str, text: str, item_id: str | None = None) -> None:
        import time

        self.type = "message"
        self.role = role
        self.id = item_id or f"id-{text[:8]}"
        self.text_content = text
        self.created_at = time.time()


class _History:
    def __init__(self, items) -> None:
        self.items = items


class _Session:
    def __init__(self, items) -> None:
        self.history = _History(items)


class Context:
    """Approval-gate context: turns simulate the conversation so far.

    Turns are (role, text) pairs, e.g. [("user", "send it"), ...].
    A plain list of strings means user turns.
    """

    def __init__(self, turns=()) -> None:
        built = []
        for turn in turns:
            if isinstance(turn, tuple):
                role, text = turn[0], turn[1]
                item_id = turn[2] if len(turn) > 2 else None
            else:
                role, text, item_id = "user", turn, None
            built.append(_Item(role, text, item_id))
        self.session = _Session(built)
