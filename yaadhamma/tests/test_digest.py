"""The scheduled Saayam digest (2026-09-27)."""

import plistlib
import sys
from pathlib import Path

import pytest
from test_whatsapp import _chat

import config
from digest import DigestStore, DigestTools, find_self_chat, run_digest
from meta_client import ModelTurn
from whatsapp import WhatsAppError, WhatsAppNotPairedError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import digest_schedule


class FakeClient:
    def __init__(self, chats, paired=True):
        self.chats = chats
        self.paired = paired
        self.sent = []
        self.read = []

    async def list_all_chats(self):
        if not self.paired:
            raise WhatsAppNotPairedError("not paired")
        return self.chats

    async def read_messages(self, name, limit, exact=False):
        self.read.append(name)
        return {
            "chat": name,
            "messages": [{"sender": "Ravi", "text": f"news in {name}"}],
        }

    async def find_chat(self, query):
        digits = "".join(ch for ch in query if ch.isdigit())
        for chat in self.chats:
            name = chat["name"]
            if (
                digits and "".join(c for c in name if c.isdigit()).endswith(digits)
            ) or (not digits and query.casefold() in name.casefold()):
                return name
        raise WhatsAppError(f"No chat {query!r}")

    async def send_message(self, name, text):
        self.sent.append((name, text))
        return {"sent": True}


class FakeBrain:
    def __init__(self, text="*Needs you*\n- Ravi needs the budget review (SC1)."):
        self.text = text
        self.calls = []

    async def generate(self, model, messages, tools, reasoning_effort=None):
        self.calls.append(messages)
        return ModelTurn(calls=[], text=self.text, tokens_in=900, tokens_out=80)


CHATS = [
    _chat("SC1-Executives", unread=3),
    _chat("SC2-Leads", unread=0),
    _chat("Family", unread=4),
    _chat("+1 (940) 843-8446 (You)"),
]


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["Saayam", "SC1", "SC2", "SC3"])
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "9408438446")


async def test_digest_summarises_watched_chats_and_sends_to_himself(tmp_path) -> None:
    client, brain = FakeClient(CHATS), FakeBrain()
    store = DigestStore(tmp_path / "db.sqlite")

    result = await run_digest(client, brain, store)

    assert result.status == "sent"
    assert client.read == ["SC1-Executives"]  # Family is not on the watchlist
    [(to, text)] = client.sent
    assert to == "+1 (940) 843-8446 (You)"
    assert "Ravi needs the budget review" in text
    assert store.latest()["status"] == "sent"


async def test_quiet_run_sends_nothing(tmp_path) -> None:
    client = FakeClient(
        [_chat("SC1-Executives", unread=0), _chat("+1 (940) 843-8446 (You)")]
    )
    store = DigestStore(tmp_path / "db.sqlite")

    result = await run_digest(client, FakeBrain(), store)

    assert result.status == "quiet"
    assert client.sent == []
    assert store.latest()["status"] == "quiet"


async def test_digest_never_sends_to_anyone_but_himself(tmp_path, monkeypatch) -> None:
    """Even if the number setting pointed at someone else, nothing is sent."""
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "0413")
    client = FakeClient([*CHATS, _chat("+1 (940) 364-0413")])

    result = await run_digest(client, FakeBrain(), DigestStore(tmp_path / "db.sqlite"))

    assert result.status == "failed"
    assert "not his own chat" in result.error
    assert client.sent == []


async def test_unpaired_digest_says_how_to_pair(tmp_path) -> None:
    result = await run_digest(
        FakeClient(CHATS, paired=False),
        FakeBrain(),
        DigestStore(tmp_path / "db.sqlite"),
    )
    assert result.status == "failed"
    assert "whatsapp_signin.py --digest" in result.error


async def test_self_chat_found_by_marker_when_no_number_set(monkeypatch) -> None:
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "")
    assert await find_self_chat(FakeClient(CHATS)) == "+1 (940) 843-8446 (You)"


async def test_voice_can_ask_for_the_latest_digest(tmp_path) -> None:
    store = DigestStore(tmp_path / "db.sqlite")
    tools = DigestTools(store)
    assert (await tools.latest_digest(None))["status"] == "none"

    await run_digest(FakeClient(CHATS), FakeBrain(), store)
    latest = await tools.latest_digest(None)
    assert latest["status"] == "sent"
    assert "budget review" in latest["summary"]


def test_schedule_runs_four_times_a_day_in_local_time(tmp_path) -> None:
    plist = digest_schedule.build_plist("/usr/local/bin/uv", project=tmp_path)
    times = [(e["Hour"], e["Minute"]) for e in plist["StartCalendarInterval"]]
    assert times == [(9, 0), (13, 0), (17, 0), (21, 0)]
    assert plist["ProgramArguments"] == [
        "/usr/local/bin/uv",
        "run",
        "scripts/digest_run.py",
    ]
    assert plist["WorkingDirectory"] == str(tmp_path)
    plistlib.dumps(plist)  # valid for macOS


async def test_delivery_test_goes_only_to_his_own_chat() -> None:
    from digest import send_test_message

    client = FakeClient(CHATS)
    chat = await send_test_message(client)
    assert chat == "+1 (940) 843-8446 (You)"
    assert client.sent == [
        ("+1 (940) 843-8446 (You)", "Yaadhamma digest test: delivery works.")
    ]


async def test_own_chat_accepted_by_full_number_without_you_label(monkeypatch) -> None:
    """Live: the chat's name label is just the number; "(You)" sits beside it."""
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "19408438446")
    client = FakeClient([_chat("+1 (940) 843-8446"), _chat("+1 (940) 364-0413")])
    assert await find_self_chat(client) == "+1 (940) 843-8446"


async def test_short_number_never_counts_as_his_own_chat(monkeypatch) -> None:
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "8446")
    client = FakeClient([_chat("+1 (940) 843-8446")])
    with pytest.raises(WhatsAppError, match="not his own chat"):
        await find_self_chat(client)


# ----------------------------------------------------------------- email


class FakeGmail:
    def __init__(self, label, emails=(), broken=False):
        self.label = label
        self.emails = list(emails)
        self.broken = broken
        self.queries = []

    def search_mail(self, query, limit=10):
        self.queries.append(query)
        if self.broken:
            raise RuntimeError("token expired")
        return self.emails


RECRUITER = {
    "from": "Priya <priya@acme.com>",
    "subject": "Interview slot for Thursday?",
    "snippet": "Could you confirm 2pm Thursday?",
    "internal_date": 2,
}


async def test_email_alone_is_enough_for_a_digest(tmp_path) -> None:
    """Jeevan: email should appear even when the Saayam chats are quiet."""
    client = FakeClient(
        [_chat("SC1-Executives", unread=0), _chat("+1 (940) 843-8446 (You)")]
    )
    brain = FakeBrain("*Needs you*\n- Priya asks you to confirm Thursday 2pm (email).")
    gmail = [FakeGmail("personal1", [RECRUITER])]

    result = await run_digest(
        client, brain, DigestStore(tmp_path / "db"), gmail_clients=gmail
    )

    assert result.status == "sent"
    assert result.emails == 1
    sent_to, text = client.sent[0]
    assert sent_to == "+1 (940) 843-8446 (You)"
    assert "Priya" in text
    payload = brain.calls[0][1]["content"]
    assert "Interview slot for Thursday?" in payload


async def test_email_search_skips_promotions_and_uses_the_last_digest_time(
    tmp_path,
) -> None:
    from datetime import datetime

    store = DigestStore(tmp_path / "db")
    client = FakeClient(
        [_chat("SC1-Executives", unread=0), _chat("+1 (940) 843-8446 (You)")]
    )
    await run_digest(
        client, FakeBrain(), store, gmail_clients=[FakeGmail("p1")]
    )  # quiet
    last = store.last_success()

    gmail = FakeGmail("p1")
    await run_digest(client, FakeBrain(), store, gmail_clients=[gmail])

    query = gmail.queries[0]
    assert "-category:promotions" in query
    assert "-category:social" in query
    assert f"after:{int(last.timestamp())}" in query
    assert isinstance(last, datetime)


async def test_one_broken_gmail_account_does_not_sink_the_digest(tmp_path) -> None:
    client = FakeClient(
        [_chat("SC1-Executives", unread=0), _chat("+1 (940) 843-8446 (You)")]
    )
    gmail = [FakeGmail("p1", broken=True), FakeGmail("p2", [RECRUITER])]

    result = await run_digest(
        client, FakeBrain(), DigestStore(tmp_path / "db"), gmail_clients=gmail
    )

    assert result.status == "sent"
    assert result.emails == 1


def test_email_window_first_run_and_cap(tmp_path) -> None:
    from datetime import datetime, timedelta

    from digest import email_since

    store = DigestStore(tmp_path / "db")
    now = datetime(2026, 9, 27, 13, 0)
    assert email_since(store, now) == now - timedelta(hours=4)  # first run


def test_old_digest_table_is_upgraded_in_place(tmp_path) -> None:
    """Jeevan's database already has a digests table without the emails column."""
    import sqlite3

    path = tmp_path / "db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE digests (started TEXT NOT NULL, finished TEXT NOT NULL, "
            "status TEXT NOT NULL, unread_chats INTEGER NOT NULL, summary TEXT NOT NULL, "
            "error TEXT NOT NULL, tokens INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO digests VALUES ('2026-09-27T12:00:00', '2026-09-27T12:01:00', "
            "'quiet', 0, 'old', '', 0)"
        )
    store = DigestStore(path)
    assert store.latest()["summary"] == "old"
    assert store.last_success().hour == 12


# ----------------------------------------------------------------- promotions / social


def test_job_mail_is_picked_out_of_promotions_and_social(monkeypatch) -> None:
    from datetime import datetime

    from digest import email_queries

    monkeypatch.setattr(config, "EMAIL_KEYWORDS", ["interview", "job offer", "Saayam"])
    main, promo = email_queries(datetime(2026, 9, 27, 9, 0))

    assert "-category:promotions" in main
    assert "{category:promotions category:social}" in promo
    assert '{interview "job offer" Saayam}' in promo
    assert "in:inbox" in promo


async def test_an_email_found_by_both_searches_is_listed_once(
    tmp_path, monkeypatch
) -> None:
    from datetime import datetime

    from digest import collect_email

    monkeypatch.setattr(config, "EMAIL_KEYWORDS", ["interview"])
    same = {**RECRUITER, "id": "abc"}
    result = await collect_email([FakeGmail("p1", [same])], datetime(2026, 9, 27))

    assert len(result["emails"]) == 1


async def test_email_preview_sends_nothing() -> None:
    from digest import preview_email

    brain = FakeBrain("*Email*\n- Priya: confirm Thursday 2pm.")
    result = await preview_email([FakeGmail("p1", [RECRUITER])], brain, hours=24)

    assert result["found"] == 1
    assert "Priya" in result["summary"]
    assert "not checked in this preview" in brain.calls[0][1]["content"]


def test_opened_emails_are_included_unless_unread_only(monkeypatch) -> None:
    """Jeevan reads email on his phone; opened mail still belongs in the digest."""
    from datetime import datetime

    from digest import email_queries

    monkeypatch.setattr(config, "EMAIL_UNREAD_ONLY", False)
    assert all("is:unread" not in q for q in email_queries(datetime(2026, 9, 27)))
    monkeypatch.setattr(config, "EMAIL_UNREAD_ONLY", True)
    assert all("is:unread" in q for q in email_queries(datetime(2026, 9, 27)))


async def test_digest_names_the_real_account_and_marks_unread() -> None:
    from datetime import datetime

    from digest import collect_email

    class Named(FakeGmail):
        def get_profile(self):
            return {"email": "deep.jeevan21@gmail.com"}

    opened = {**RECRUITER, "id": "x1", "is_read": True}
    result = await collect_email([Named("personal2", [opened])], datetime(2026, 9, 27))

    [email] = result["emails"]
    assert email["account"] == "deep.jeevan21@gmail.com"
    assert email["unread"] is False


async def test_opened_emails_are_included_and_marked(tmp_path) -> None:
    from datetime import datetime

    from digest import collect_email

    opened = {**RECRUITER, "id": "r1", "is_read": True}
    fresh = {
        **RECRUITER,
        "id": "r2",
        "subject": "New role",
        "is_read": False,
        "internal_date": 3,
    }
    result = await collect_email(
        [FakeGmail("p1", [opened, fresh])], datetime(2026, 9, 27)
    )

    flags = {e["subject"]: e["unread"] for e in result["emails"]}
    assert flags == {"Interview slot for Thursday?": False, "New role": True}


async def test_preview_warns_when_one_account_is_linked_twice() -> None:
    from digest import preview_email

    class Named(FakeGmail):
        def __init__(self, label, address):
            super().__init__(label)
            self.address = address

        def get_profile(self):
            return {"email": self.address}

    clients = [
        Named("personal1", "deep.jeevan98@gmail.com"),
        Named("personal2", "deep.jeevan21@gmail.com"),
        Named("personal3", "deep.jeevan98@gmail.com"),
    ]
    result = await preview_email(clients, FakeBrain(), hours=24)

    assert result["duplicate_accounts"] == ["deep.jeevan98@gmail.com"]
