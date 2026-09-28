#!/usr/bin/env python3
"""Run the Yaadhamma task scorecard and print the card.

Every task drives the REAL tool code against fake backends: no messages
are sent, no mail is moved, no paid model is called, and all costs shown
are simulated from the fake token counts.

Release rule: 90%+ pass AND every injected fault recovered before the
owner runs the 10-task spoken acceptance.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(PROJECT / "tests" / "scorecard"))

from cases import register  # noqa: E402

from scorecard import CaseContext, Scorecard, format_report  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yaadhamma-scorecard-") as tmp:
        card = Scorecard()
        register(card)
        print(f"Running {len(card.cases)} scorecard tasks (fake backends only)...\n")

        def make_ctx() -> CaseContext:
            ctx = CaseContext()
            ctx.tmp_path = Path(tmp)  # type: ignore[attr-defined]
            return ctx

        report = asyncio.run(card.run(make_ctx=make_ctx))
    print(format_report(report))
    go, _ = report.verdict()
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())
