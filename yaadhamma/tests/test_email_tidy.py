"""Tests for Gmail labels and archiving (src/email_tidy.py).

No network, no real Gmail: a fake Gmail client and an injected classifier.
Real Gmail behaviour is unverified (see docs/BUILD_REPORT.md).
"""

from datetime import datetime, timedelta

from email_tidy import (
    LABELS,
    EmailTidy,
    classify_email,
    is_person,
    should_archive,
)


def _meta(**over):
    if "from_" in over:
        over["from"] = over.pop("from_")
    meta = {
        "id": "m1",
        "from": "Acme <contact@acme.com>",
        "subject": "New roles for you",
        "date": datetime(2026, 9, 28, 10, 0, 0),
        "list_unsubscribe": False,
    }
    meta.update(over)
    return meta


def _classifier(mapping):
    def classify(meta):
        return mapping.get(meta["id"], "none")

    return classify


# ---------------------------------------------------------------- classify


def test_classification_mapping_rules() -> None:
    jobs = _meta(subject="Your application for ML Engineer")
    assert classify_email(jobs) == "Yaadhamma/Jobs"
    finance = _meta(from_="Bank <alerts@bank.com>", subject="Your statement is ready")
    assert classify_email(finance) == "Yaadhamma/Finance"
    receipt = _meta(subject="Receipt for order #1234")
    assert classify_email(receipt) == "Yaadhamma/Receipts"
    news = _meta(
        from_="News <news@example.com>",
        subject="Weekly digest",
        list_unsubscribe=True,
    )
    assert classify_email(news) == "Yaadhamma/Newsletters"
    saayam = _meta(from_="Saayam <team@saayamforall.org>", subject="All-hands notes")
    assert classify_email(saayam) == "Yaadhamma/Saayam"


def test_cheap_model_breaks_ties() -> None:
    meta = _meta(from_="Friend <a@b.com>", subject="lunch tomorrow", id="m9")
    # No rule fires; the cheap model decides.
    assert classify_email(meta, _classifier({"m9": "Yaadhamma/Jobs"})) == (
        "Yaadhamma/Jobs"
    )
    # A personal mail the model says is nothing stays unlabelled.
    assert classify_email(meta, _classifier({"m9": "none"})) is None


def test_unknown_label_from_model_is_rejected() -> None:
    meta = _meta(from_="x@y.com", subject="???", id="m2")
    assert classify_email(meta, _classifier({"m2": "Yaadhamma/Spam"})) is None


# ---------------------------------------------------------------- persons


def test_person_detection() -> None:
    assert is_person(_meta(from_="Ravi Kumar <ravi@example.com>"))
    assert not is_person(_meta(from_="News <newsletter@acme.com>"))
    assert not is_person(_meta(list_unsubscribe=True))
    assert not is_person(_meta(from_="noreply@bank.com", subject="statement"))


def test_person_email_never_archived() -> None:
    now = datetime(2026, 9, 28, 12, 0, 0)
    old = now - timedelta(days=30)
    meta = _meta(
        from_="Ravi Kumar <ravi@example.com>",
        subject="Weekly newsletter",
        date=old,
        list_unsubscribe=True,
    )
    assert should_archive(meta, "Yaadhamma/Newsletters", now) is False


# ---------------------------------------------------------------- 7-day rule


def test_seven_day_rule() -> None:
    now = datetime(2026, 9, 28, 12, 0, 0)
    old = _meta(
        from_="Store <deals@store.com>",
        subject="Sale ends soon",
        date=now - timedelta(days=8),
        list_unsubscribe=True,
    )
    assert should_archive(old, "Yaadhamma/Newsletters", now) is True
    fresh = _meta(
        from_="Store <deals@store.com>",
        subject="Sale ends soon",
        date=now - timedelta(days=2),
        list_unsubscribe=True,
    )
    assert should_archive(fresh, "Yaadhamma/Newsletters", now) is False
    # Boundary: exactly 7 days is not older than 7 days.
    edge = _meta(
        from_="Store <deals@store.com>",
        subject="Sale",
        date=now - timedelta(days=7),
        list_unsubscribe=True,
    )
    assert should_archive(edge, "Yaadhamma/Newsletters", now) is False


def test_unlabelled_never_archived() -> None:
    now = datetime(2026, 9, 28, 12, 0, 0)
    old = _meta(
        from_="Store <deals@store.com>",
        subject="Sale",
        date=now - timedelta(days=30),
    )
    assert should_archive(old, None, now) is False
    assert should_archive(old, "Yaadhamma/Jobs", now) is False


# ---------------------------------------------------------------- run modes


class FakeGmail:
    def __init__(self):
        self.label = "personal1"
        self.labels = {}  # name -> id
        self.created = []
        self.modified = []  # (message_id, add, remove)
        self.inbox = []  # message metas

    def list_labels(self):
        return dict(self.labels)

    def create_label(self, name):
        self.created.append(name)
        if name not in self.labels:
            self.labels[name] = f"L{len(self.labels)}"
        return self.labels[name]

    def modify_message(self, message_id, add=(), remove=()):
        self.modified.append((message_id, list(add), list(remove)))
        return {"id": message_id}

    def search_mail(self, query, limit=50):
        return list(self.inbox)

    def get_message(self, message_id):
        for meta in self.inbox:
            if meta["id"] == message_id:
                return meta
        raise KeyError(message_id)


def _tidy(monkeypatch, tmp_path, mode="propose"):
    import json

    monkeypatch.setenv("YAADHAMMA_EMAIL_TIDY", mode)
    monkeypatch.setenv("HOME", str(tmp_path))
    # The fake account is linked with the label-modifying scope.
    token_dir = tmp_path / ".yaadhamma"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "gmail-token-personal1.json").write_text(
        json.dumps({"scope": "https://www.googleapis.com/auth/gmail.modify"})
    )
    client = FakeGmail()
    tidy = EmailTidy(
        clients=[client],
        classify_fn=_classifier({}),
        now=lambda: datetime(2026, 9, 28, 12, 0, 0),
    )
    return client, tidy


def test_propose_mode_changes_nothing(monkeypatch, tmp_path) -> None:
    import asyncio

    client, tidy = _tidy(monkeypatch, tmp_path, "propose")
    client.inbox = [
        _meta(id="m1", subject="Your application for ML Engineer"),
        _meta(
            id="m2",
            from_="Store <deals@store.com>",
            subject="Sale",
            date=datetime(2026, 9, 10, 12, 0, 0),
            list_unsubscribe=True,
        ),
    ]
    report = asyncio.run(tidy.run())
    assert client.created == []
    assert client.modified == []
    assert report["mode"] == "propose"
    assert report["labelled"] == 2  # m1 Jobs, m2 Newsletters
    assert report["archived"] == 1  # m2, older than 7 days
    proposal = tmp_path / "Documents" / "Yaadhamma"
    assert list(proposal.glob("email-tidy-proposal-*.md"))


def test_apply_mode_labels_and_archives(monkeypatch, tmp_path) -> None:
    import asyncio

    client, tidy = _tidy(monkeypatch, tmp_path, "apply")
    client.inbox = [
        _meta(id="m1", subject="Your application for ML Engineer"),
        _meta(
            id="m2",
            from_="Store <deals@store.com>",
            subject="Sale",
            date=datetime(2026, 9, 10, 12, 0, 0),
            list_unsubscribe=True,
        ),
    ]
    report = asyncio.run(tidy.run())
    assert "Yaadhamma/Jobs" in client.created
    assert client.modified  # label added to m1, INBOX removed from m2
    by_id = {m[0]: m for m in client.modified}
    assert "INBOX" in by_id["m2"][2]
    assert report["archived"] == 1
    log = tmp_path / ".yaadhamma" / "email-tidy.log"
    assert log.exists()


def test_label_creation_idempotent(monkeypatch, tmp_path) -> None:
    import asyncio

    client, tidy = _tidy(monkeypatch, tmp_path, "apply")
    client.labels["Yaadhamma/Jobs"] = "L0"
    client.inbox = [_meta(id="m1", subject="Your application for ML Engineer")]
    asyncio.run(tidy.run())
    assert client.created == []  # already there: not created again


def test_labels_constant() -> None:
    assert LABELS == [
        "Yaadhamma/Jobs",
        "Yaadhamma/Saayam",
        "Yaadhamma/Finance",
        "Yaadhamma/Receipts",
        "Yaadhamma/Newsletters",
    ]
