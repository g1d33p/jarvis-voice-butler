"""Nightly file tidy-up (2026-09-28)."""

import json
import os
from datetime import datetime, timedelta

import pytest

import tidy
from meta_client import ModelTurn
from tidy import (
    TidyLog,
    apply_plan,
    build_plan,
    candidates,
    plan_summary,
    write_plan_file,
)

NOW = datetime(2026, 9, 29, 3, 0)


def _file(folder, name, days_old=3, text="x"):
    path = folder / name
    path.write_text(text)
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


class FakeBrain:
    def __init__(self, decisions):
        self.decisions = decisions
        self.calls = []

    async def generate(
        self, model, messages, tools, reasoning_effort=None, feature="test"
    ):
        self.calls.append(json.loads(messages[1]["content"]))
        return ModelTurn(calls=[], text=json.dumps({"files": self.decisions}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(tidy, "SORTED_ROOT", tmp_path / "Documents" / "Sorted")
    return downloads


def test_recent_hidden_folders_and_new_installers_are_left_alone(home) -> None:
    _file(home, "old notes.txt", days_old=3)
    _file(home, "today.txt", days_old=0)
    _file(home, ".hidden", days_old=30)
    _file(home, "Zoom.pkg", days_old=2)
    _file(home, "Old app.dmg", days_old=30)
    (home / "Project folder").mkdir()

    picked, left = candidates([home], NOW)

    assert sorted(p.name for p in picked) == ["Old app.dmg", "old notes.txt"]
    assert any("today.txt" in item for item in left)
    assert any("Zoom.pkg" in item for item in left)


async def test_plan_groups_by_topic_and_renames_only_vague_names(home) -> None:
    vague = _file(
        home, "Document (3).txt", text="Offer letter from Acme Corp for Jeevan"
    )
    clear = _file(home, "Saayam roadmap.pdf")
    other = _file(home, "IMG_20260901.heic")
    brain = FakeBrain(
        [
            {
                "path": str(vague),
                "group": "Job search",
                "new_name": "2026-09-26 Offer letter - Acme",
            },
            {"path": str(clear), "group": "Saayam", "new_name": "Should be ignored"},
        ]
    )

    plan = await build_plan(brain, folders=[home], now=NOW)

    by_source = {os.path.basename(m.source): m for m in plan.moves}
    offer = by_source["Document (3).txt"]
    assert offer.group == "Job search" and offer.renamed
    assert offer.destination.endswith(
        "Sorted/Job search/2026-09-26 Offer letter - Acme.txt"
    )
    roadmap = by_source["Saayam roadmap.pdf"]
    assert roadmap.destination.endswith(
        "Sorted/Saayam/Saayam roadmap.pdf"
    )  # clear name kept
    assert not roadmap.renamed
    assert by_source["IMG_20260901.heic"].group == "Images"  # no topic: by type
    # The vague file's content was used to understand it; clear files were not read.
    listing = {f["name"]: f for f in brain.calls[0]["files"]}
    assert "Offer letter" in listing["Document (3).txt"]["snippet"]
    assert "snippet" not in listing["Saayam roadmap.pdf"]
    assert other.exists()  # planning moves nothing


async def test_two_files_with_the_same_target_name_never_collide(home) -> None:
    a = _file(home, "Document.txt")
    b = _file(home, "Document (1).txt")
    brain = FakeBrain(
        [
            {"path": str(a), "group": "Personal documents", "new_name": "Lease"},
            {"path": str(b), "group": "Personal documents", "new_name": "Lease"},
        ]
    )
    plan = await build_plan(brain, folders=[home], now=NOW)
    targets = sorted(os.path.basename(m.destination) for m in plan.moves)
    assert targets == ["Lease 2.txt", "Lease.txt"]


async def test_applying_moves_logs_and_never_overwrites(home, tmp_path) -> None:
    resume = _file(home, "Resume_Jeevan.pdf")
    brain = FakeBrain([{"path": str(resume), "group": "Job search"}])
    plan = await build_plan(brain, folders=[home], now=NOW)
    existing = tidy.SORTED_ROOT / "Job search" / "Resume_Jeevan.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_text("older copy")
    log = TidyLog(tmp_path / "db")
    log.save_plan(plan)

    done = apply_plan(plan, log)

    assert done["moved"] == 1
    assert existing.read_text() == "older copy"  # untouched
    assert (tidy.SORTED_ROOT / "Job search" / "Resume_Jeevan 2.pdf").exists()
    assert not resume.exists()
    [found] = log.find_moved("resume")
    assert found["was"].endswith("Downloads/Resume_Jeevan.pdf")
    assert log.latest_plan()[0] == "applied"


async def test_plan_file_and_summary_are_readable(home, tmp_path) -> None:
    doc = _file(home, "notes.txt")
    plan = await build_plan(
        FakeBrain([{"path": str(doc), "group": "Saayam"}]), folders=[home], now=NOW
    )
    path = write_plan_file(plan, folder=tmp_path / "plans")

    text = path.read_text()
    assert "## Saayam (1)" in text and "notes.txt" in text
    assert "Nothing is deleted" in text
    assert "Tidy plan ready: 1 files (Saayam 1)" in plan_summary(plan)


def test_tidy_runs_at_three_am() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import digest_schedule

    jobs = {label: (times, extra) for label, times, extra, _ in digest_schedule.JOBS}
    assert jobs["com.yaadhamma.tidy"] == ([(3, 0)], ["--tidy"])
