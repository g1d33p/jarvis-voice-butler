"""Stage 8: the file tidy defaults to apply; YAADHAMMA_TIDY=propose opts out.

TIDY_MODE is read at import, so these tests reload config under a patched
environment. Guards (never delete, never overwrite, every move verified)
live in tidy.apply_plan and are covered by the existing tidy tests.
"""

import importlib


def _tidy_mode(monkeypatch, **env):
    for key in ("YAADHAMMA_TIDY", "YAADHAMMA_TIDY_MODE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import config

    return importlib.reload(config).TIDY_MODE


def test_default_is_apply(monkeypatch):
    assert _tidy_mode(monkeypatch) == "apply"


def test_propose_opt_out(monkeypatch):
    assert _tidy_mode(monkeypatch, YAADHAMMA_TIDY="propose") == "propose"


def test_legacy_var_still_honoured(monkeypatch):
    assert _tidy_mode(monkeypatch, YAADHAMMA_TIDY_MODE="propose") == "propose"


def test_unknown_value_stays_safe(monkeypatch):
    assert _tidy_mode(monkeypatch, YAADHAMMA_TIDY="bogus") == "apply"


def test_3am_job_runs_file_and_email_tidy():
    import sys

    sys.path.insert(0, "scripts")
    import digest_schedule

    jobs = {label: args for label, _, args, _ in digest_schedule.JOBS}
    assert jobs["com.yaadhamma.tidy"] == ["--tidy", "--email-tidy"]
