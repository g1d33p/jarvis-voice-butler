"""Stage 7: commitments with due dates, reminders, explicit completion.

Fake clock, tmp HOME; the real store code runs against a throwaway DB.
"""

from datetime import datetime

from planner import parse_due_date
from task_store import CommitmentStore

# Monday 2026-09-28 10:00 local — the anchor for every due-date test.
NOW = datetime(2026, 9, 28, 10, 0, 0)


def test_by_5pm_is_today():
    assert parse_due_date("call Ravi back by 5pm", NOW) == datetime(2026, 9, 28, 17, 0)


def test_by_530pm():
    assert parse_due_date("send the deck by 5:30pm", NOW) == datetime(
        2026, 9, 28, 17, 30
    )


def test_tomorrow_defaults_to_end_of_day():
    assert parse_due_date("pay the bill tomorrow", NOW) == datetime(2026, 9, 29, 18, 0)


def test_tomorrow_with_time():
    assert parse_due_date("dentist tomorrow 9am", NOW) == datetime(2026, 9, 29, 9, 0)


def test_next_monday_with_time():
    assert parse_due_date("review the PR Monday 9am", NOW) == datetime(
        2026, 10, 5, 9, 0
    )


def test_bare_weekday_defaults_to_end_of_day():
    assert parse_due_date("call the bank Monday", NOW) == datetime(2026, 10, 5, 18, 0)


def test_month_day_rolls_to_next_year_when_passed():
    assert parse_due_date("renew the domain Jan 5", NOW) == datetime(2027, 1, 5, 18, 0)


def test_month_day_this_year_when_upcoming():
    assert parse_due_date("flight Dec 20", NOW) == datetime(2026, 12, 20, 18, 0)


def test_month_day_keeps_valid_31st():
    # Regression: the old code clamped every day above 28 to 28.
    assert parse_due_date("party Jan 31", NOW) == datetime(2027, 1, 31, 18, 0)


def test_month_day_clamps_only_to_real_last_day():
    # Feb 30 is not a date; the month's last day is the sensible fallback.
    assert parse_due_date("review Feb 30", NOW) == datetime(2027, 2, 28, 18, 0)


def test_end_of_week_is_friday():
    assert parse_due_date("finish the report end of week", NOW) == datetime(
        2026, 10, 2, 18, 0
    )


def test_ambiguous_has_no_due_date():
    assert parse_due_date("look into that soon", NOW) is None
    assert parse_due_date("call me when you can", NOW) is None
    assert parse_due_date("remind me sometime", NOW) is None


def test_no_date_words_has_no_due_date():
    assert parse_due_date("call Ravi back", NOW) is None


# -- store -----------------------------------------------------------------


def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return CommitmentStore()


def test_commit_and_pending(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    task = store.committed_task(
        "call Ravi back", datetime(2026, 9, 28, 17, 0), "promised on the phone"
    )
    assert task["text"] == "call Ravi back"
    assert task["due_at"] == "2026-09-28T17:00:00"
    assert task["status"] == "pending"
    assert len(store.pending()) == 1


def test_commit_without_due_date(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    task = store.committed_task("read the book", None, "voice")
    assert task["due_at"] is None
    assert len(store.pending()) == 1


def test_due_today_and_overdue(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.committed_task("due today", datetime(2026, 9, 28, 17, 0), "voice")
    store.committed_task("overdue", datetime(2026, 9, 27, 12, 0), "voice")
    store.committed_task("later", datetime(2026, 9, 30, 12, 0), "voice")
    assert [t["text"] for t in store.due_today(NOW)] == ["due today"]
    assert [t["text"] for t in store.overdue(NOW)] == ["overdue"]


def test_mark_done_by_id(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    task = store.committed_task("call Ravi back", None, "voice")
    done = store.mark_done(task_id=task["id"])
    assert done["status"] == "completed"
    assert store.pending() == []


def test_mark_done_by_text_matches_one(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.committed_task("call Ravi back", None, "voice")
    done = store.mark_done(text="ravi")
    assert done["status"] == "completed"


def test_mark_done_by_text_needs_exactly_one_match(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.committed_task("call Ravi", None, "voice")
    store.committed_task("email Ravi", None, "voice")
    try:
        store.mark_done(text="ravi")
    except LookupError as exc:
        assert "2" in str(exc)
    else:
        raise AssertionError("ambiguous match should raise")
    assert len(store.pending()) == 2  # nothing completed by accident


def test_mark_done_unknown_raises(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    try:
        store.mark_done(task_id="nope")
    except LookupError:
        pass
    else:
        raise AssertionError("unknown id should raise")


def test_completed_stays_out_of_reminders(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    task = store.committed_task("old thing", datetime(2026, 9, 20, 12, 0), "voice")
    store.mark_done(task_id=task["id"])
    assert store.overdue(NOW) == []
    assert store.due_today(NOW) == []


# -- reminders --------------------------------------------------------------


def test_wake_review_text(monkeypatch, tmp_path):
    from task_store import wake_review_text

    store = _store(monkeypatch, tmp_path)
    store.committed_task("pay the bill", datetime(2026, 9, 27, 12, 0), "voice")
    store.committed_task("call Ravi", datetime(2026, 9, 28, 17, 0), "voice")
    text = wake_review_text(now=NOW)
    assert "pay the bill" in text
    assert "call Ravi" in text
    assert "overdue" in text.lower()


def test_wake_review_empty_when_nothing_due(monkeypatch, tmp_path):
    from task_store import wake_review_text

    _store(monkeypatch, tmp_path)
    assert wake_review_text(now=NOW) == ""


def test_morning_brief_commitments_line(monkeypatch, tmp_path):
    from digest import _commitments_line

    store = _store(monkeypatch, tmp_path)
    store.committed_task("call Ravi", datetime(2026, 9, 28, 17, 0), "voice")
    line = _commitments_line(now=NOW)
    assert "Commitments due today" in line
    assert "call Ravi" in line


def test_morning_brief_commitments_line_empty(monkeypatch, tmp_path):
    from digest import _commitments_line

    _store(monkeypatch, tmp_path)
    assert _commitments_line(now=NOW) == ""
