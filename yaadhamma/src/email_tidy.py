"""Gmail labels and archiving: the nightly email tidy.

Labels (created on demand, nested under Yaadhamma/):
    Yaadhamma/Jobs, Yaadhamma/Saayam, Yaadhamma/Finance,
    Yaadhamma/Receipts, Yaadhamma/Newsletters

Each run:
  1. Classifies NEW inbox mail since the last run (cheap rules first, then
     the cheap model for the rest) and applies one label.
  2. Archives promotions/newsletters OLDER THAN 7 DAYS (removes the INBOX
     label only).

Hard rules, enforced in code:
  - Never delete. Never mark as read. Never archive anything unlabelled or
    from a person.
  - First run is propose-only: the plan is written to
    ~/Documents/Yaadhamma/ and summarised for the morning brief.
    YAADHAMMA_EMAIL_TIDY=apply enables real changes.

Every change is logged to ~/.yaadhamma/email-tidy.log (JSONL) so it can be
reported and reversed by hand: re-add the INBOX label to un-archive, remove
a Yaadhamma/* label to un-label.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger("yaadhamma.email_tidy")

LABELS = [
    "Yaadhamma/Jobs",
    "Yaadhamma/Saayam",
    "Yaadhamma/Finance",
    "Yaadhamma/Receipts",
    "Yaadhamma/Newsletters",
]

ARCHIVE_AFTER_DAYS = 7
# Only this label is ever archived (promotional mail is classified under it).
ARCHIVABLE_LABEL = "Yaadhamma/Newsletters"

BULK_SENDERS = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "newsletter",
    "news",
    "updates",
    "alerts",
    "notifications",
    "support",
    "billing",
    "info",
    "hello",
    "team",
    "deals",
    "offers",
    "promo",
    "marketing",
    "jobs",
    "careers",
    "press",
    "media",
}

_RULES: list[tuple[str, re.Pattern]] = [
    (
        "Yaadhamma/Receipts",
        re.compile(r"receipt|invoice|order confir|payment recei|your bill", re.I),
    ),
    (
        "Yaadhamma/Finance",
        re.compile(
            r"statement|account alert|payment due|transaction|bank|credit card|investment",
            re.I,
        ),
    ),
    (
        "Yaadhamma/Jobs",
        re.compile(
            r"\bjob\b|jobs|interview|application|recruiter|hiring|offer letter|applied for",
            re.I,
        ),
    ),
    ("Yaadhamma/Saayam", re.compile(r"saayam", re.I)),
]


def _sender_parts(meta: dict) -> tuple[str, str]:
    """(display name, address) from a From header."""
    raw = str(meta.get("from", ""))
    match = re.match(r"\s*(.*?)\s*<([^<>]+)>\s*$", raw)
    if match:
        return match.group(1).strip(' "'), match.group(2).strip().lower()
    return "", raw.strip().lower()


def is_person(meta: dict) -> bool:
    """Is this from a human being rather than bulk mail?

    A bulk sender name or newsletter-ish address disqualifies; otherwise a
    human-looking display name or personal address qualifies — even with a
    List-Unsubscribe header (a personal newsletter is still from a person,
    and people are never archived).
    """
    name, address = _sender_parts(meta)
    local = address.split("@")[0]
    if local in BULK_SENDERS or "newsletter" in local or "digest" in local:
        return False
    if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+)+", name):
        return True
    # firstname.lastname@ — plainly a personal address
    return bool(re.fullmatch(r"[a-z]+\.[a-z]+", local))


def classify_email(meta: dict, classify_fn=None) -> str | None:
    """One Yaadhamma/* label for a message, or None.

    Deterministic rules fire first (free, testable); anything they miss goes
    to the cheap model via `classify_fn`, which must return a label name or
    "none". A model answer outside the known labels is rejected.
    """
    if meta.get("list_unsubscribe"):
        return "Yaadhamma/Newsletters"
    haystack = f"{meta.get('from', '')} {meta.get('subject', '')}"
    for label, pattern in _RULES:
        if pattern.search(haystack):
            return label
    _name, address = _sender_parts(meta)
    local = address.split("@")[0]
    if local in BULK_SENDERS or "newsletter" in local or "digest" in local:
        return "Yaadhamma/Newsletters"
    if classify_fn is None:
        return None
    try:
        answer = str(classify_fn(meta)).strip()
    except Exception:
        return None
    return answer if answer in LABELS else None


def should_archive(meta: dict, label: str | None, now: datetime) -> bool:
    """Archive = remove the INBOX label. Only old newsletters, never people."""
    if label != ARCHIVABLE_LABEL:
        return False
    if is_person(meta):
        return False
    date = meta.get("date")
    if not isinstance(date, datetime):
        return False
    return (now - date) > timedelta(days=ARCHIVE_AFTER_DAYS)


def cheap_model_classifier():
    """The real classifier: the cheap Gemini model, strict single-label."""
    import config
    from meta_client import brain_client_from_config

    client = brain_client_from_config()
    model = config.BRAIN_MODEL
    prompt = (
        "Classify this email into exactly one label: "
        + ", ".join(LABELS)
        + ', or "none". Reply with only the label.\n'
    )

    async def classify(meta: dict) -> str:
        messages = [
            {
                "role": "user",
                "content": prompt
                + f"From: {meta.get('from', '')}\n"
                + f"Subject: {meta.get('subject', '')}\n",
            }
        ]
        turn = await client.generate(model, messages, tools=[], feature="email-tidy")
        return (turn.text or "").strip().split("\n")[0].strip()

    # The tidy runs synchronously per message; bridge the async model call.
    def classify_sync(meta: dict) -> str:
        try:
            return asyncio.run(classify(meta))
        except Exception:
            return "none"

    return classify_sync


@dataclass
class EmailTidy:
    clients: list
    classify_fn: Callable[[dict], str] | None = None
    now: Callable[[], datetime] = datetime.now

    def __post_init__(self) -> None:
        self._log_path = Path.home() / ".yaadhamma" / "email-tidy.log"
        self._state_path = Path.home() / ".yaadhamma" / "email-tidy-state.json"

    # ------------------------------------------------------------------ run

    async def run(self) -> dict:
        import config

        settings = config.email_tidy_settings()
        mode = settings["mode"]
        now = self.now()
        report = {
            "mode": mode,
            "labelled": 0,
            "archived": 0,
            "accounts": 0,
            "actions": [],  # dicts: account/message_id/action/label/from/subject
        }
        self._check_scopes()
        since = self._last_run()
        label_ids: dict[str, dict[str, str]] = {}
        for client in self.clients:
            report["accounts"] += 1
            label_ids[client.label] = {}
            inbox = self._new_inbox(client, since)
            for meta in inbox:
                label = classify_email(meta, self.classify_fn)
                action = {"account": client.label, "message_id": meta["id"]}
                action.update(
                    {
                        "from": str(meta.get("from", ""))[:120],
                        "subject": str(meta.get("subject", ""))[:120],
                    }
                )
                if label is not None:
                    lid = self._label_id(client, label_ids[client.label], label, mode)
                    action["action"] = "label"
                    action["label"] = label
                    if mode == "apply":
                        client.modify_message(meta["id"], add=[lid])
                        self._log_change(action, reverse=f"remove label {label}")
                    report["labelled"] += 1
                    report["actions"].append(action)
                # Archive check applies to every inbox message seen, old or new.
                if should_archive(meta, label, now):
                    arch = dict(action)
                    arch["action"] = "archive"
                    arch["label"] = label
                    if mode == "apply":
                        client.modify_message(meta["id"], remove=["INBOX"])
                        self._log_change(arch, reverse="re-add the INBOX label")
                    report["archived"] += 1
                    report["actions"].append(arch)
        self._write_state(now, report, mode)
        return report

    # --------------------------------------------------------------- helpers

    def _check_scopes(self) -> None:
        from gmail import GmailAuthError, has_modify_scope

        lacking = [
            client.label
            for client in self.clients
            if not has_modify_scope(client.label)
        ]
        if lacking:
            names = ", ".join(lacking)
            raise GmailAuthError(
                f"Gmail label changes need the gmail.modify scope; {names} "
                f"was linked without it. Re-link each account:\n"
                + "\n".join(
                    f"  uv run scripts/gmail_signin.py {label}" for label in lacking
                )
            )

    def _last_run(self) -> datetime | None:
        try:
            data = json.loads(self._state_path.read_text())
            return datetime.fromisoformat(data["last_run"])
        except Exception:
            return None

    def _new_inbox(self, client, since: datetime | None) -> list[dict]:
        query = "in:inbox" if since is None else f"in:inbox after:{since:%Y/%m/%d}"
        try:
            found = client.search_mail(query, limit=50)
        except Exception as exc:
            log.warning("email-tidy: search failed for %s: %s", client.label, exc)
            return []
        metas = []
        for item in found:
            try:
                metas.append(client.get_message(item["id"]))
            except Exception as exc:
                log.warning("email-tidy: skipping %s: %s", item.get("id"), exc)
        return metas

    def _label_id(self, client, cache: dict[str, str], name: str, mode: str) -> str:
        if name not in cache:
            existing = client.list_labels()
            if name in existing:
                cache[name] = existing[name]
            elif mode == "apply":
                cache[name] = client.create_label(name)
            else:
                cache[name] = f"pending:{name}"
        return cache[name]

    def _log_change(self, action: dict, reverse: str) -> None:
        entry = {
            "ts": self.now().isoformat(),
            "reverse_by_hand": reverse,
            **action,
        }
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _write_state(self, now: datetime, report: dict, mode: str) -> None:
        # Last-run watermark (only advanced on a real run, propose or apply).
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"last_run": now.isoformat(), "mode": mode})
            )
        except Exception:
            pass
        # Proposal document on propose mode.
        if mode == "propose" and report["actions"]:
            self._write_proposal(now, report)
        # Summary for the morning brief.
        try:
            summary_path = Path.home() / ".yaadhamma" / "email-tidy-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "date": now.date().isoformat(),
                        "mode": mode,
                        "labelled": report["labelled"],
                        "archived": report["archived"],
                        "accounts": report["accounts"],
                    }
                )
            )
        except Exception:
            pass

    def _write_proposal(self, now: datetime, report: dict) -> Path:
        docs = Path.home() / "Documents" / "Yaadhamma"
        docs.mkdir(parents=True, exist_ok=True)
        path = docs / f"email-tidy-proposal-{now:%Y-%m-%d}.md"
        lines = [
            f"# Email tidy proposal — {now:%A %d %B %Y}",
            "",
            "Propose-only run: nothing was changed. Set",
            "`YAADHAMMA_EMAIL_TIDY=apply` to let the nightly run apply these.",
            "",
            "| Account | Action | Label | From | Subject |",
            "| --- | --- | --- | --- | --- |",
        ]
        for action in report["actions"]:
            lines.append(
                f"| {action['account']} | {action['action']} | "
                f"{action.get('label', '—')} | {action['from']} | "
                f"{action['subject']} |"
            )
        lines += [
            "",
            "To reverse any applied change by hand: re-add the INBOX label to",
            "un-archive, or remove the Yaadhamma/* label to un-label.",
        ]
        path.write_text("\n".join(lines) + "\n")
        return path


async def main_async() -> int:
    from gmail import GmailClient, discover_labels

    labels = discover_labels()
    if not labels:
        print("No Gmail accounts linked. Run scripts/gmail_signin.py first.")
        return 1
    tidy = EmailTidy(
        clients=[GmailClient(label=label) for label in labels],
        classify_fn=cheap_model_classifier(),
    )
    try:
        report = await tidy.run()
    except Exception as exc:
        print(f"email tidy failed: {exc}")
        return 1
    print(
        f"email tidy ({report['mode']}): "
        f"{report['labelled']} labelled, {report['archived']} archived, "
        f"across {report['accounts']} account(s)."
    )
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
