"""Task scorecard: realistic tasks run against fake clients.

Each case drives the REAL tool code (GmailTools, WhatsAppTools, ...) with a
fake backend, so the scorecard exercises the production paths — approval
gates, verification, retries — without touching the real world: no messages
sent, no mail moved, no paid model calls.

Costs are SIMULATED: fakes report the tokens an operation would have used and
the runner prices them with costs.PRICE_PER_1K. The card measures behaviour
and recovery, not real spend.

Release rule (see scripts/scorecard.py): 90%+ pass and recovery proven
before the owner does the 10-task spoken acceptance.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Callable

PASS_RATE_THRESHOLD = 0.9
RECOVERY_RATE_THRESHOLD = 1.0


class CaseContext:
    """What a case gets: fault watching and simulated-cost notes."""

    def __init__(self) -> None:
        self._fakes: list[object] = []
        self.cost_usd = 0.0
        self.cost_notes: list[str] = []

    def watch(self, fake: object) -> object:
        """Register a fake so its fired faults count toward recovery."""
        self._fakes.append(fake)
        return fake

    def note_cost(self, usd: float, why: str) -> None:
        self.cost_usd += usd
        self.cost_notes.append(f"${usd:.4f} {why}")

    @property
    def faults_fired(self) -> int:
        return sum(int(getattr(f, "faults_fired", 0) or 0) for f in self._fakes)


@dataclass
class TaskCase:
    name: str
    category: str
    run: Callable[[CaseContext], Awaitable[str]]
    expect_failure: bool = False


@dataclass
class CaseResult:
    name: str
    category: str
    ok: bool
    detail: str
    seconds: float
    cost_usd: float
    faults_fired: int = 0
    recovered: bool = False


@dataclass
class ScorecardReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def fault_cases(self) -> list[CaseResult]:
        return [r for r in self.results if r.faults_fired > 0]

    @property
    def recovery_rate(self) -> float:
        faulted = self.fault_cases
        if not faulted:
            return 1.0  # vacuously perfect: nothing needed recovering
        return sum(1 for r in faulted if r.ok and r.recovered) / len(faulted)

    @property
    def total_seconds(self) -> float:
        return sum(r.seconds for r in self.results)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    def verdict(
        self,
        min_pass_rate: float = PASS_RATE_THRESHOLD,
        min_recovery_rate: float = RECOVERY_RATE_THRESHOLD,
    ) -> tuple[bool, str]:
        """The release rule: high pass rate AND recovery proven."""
        if self.pass_rate < min_pass_rate:
            return False, (
                f"NO-GO: pass rate {self.pass_rate:.0%} is below {min_pass_rate:.0%}."
            )
        if self.recovery_rate < min_recovery_rate:
            return False, (
                f"NO-GO: recovery rate {self.recovery_rate:.0%} is below "
                f"{min_recovery_rate:.0%} — injected faults were not recovered."
            )
        return True, (
            f"GO: pass rate {self.pass_rate:.0%} and recovery "
            f"{self.recovery_rate:.0%} meet the release rule."
        )


class Scorecard:
    """Collects cases (via the @card.case decorator) and runs them."""

    def __init__(self) -> None:
        self._cases: list[TaskCase] = []

    def case(
        self,
        name: str,
        category: str,
        expect_failure: bool = False,
    ) -> Callable[[Callable[[CaseContext], Awaitable[str]]], TaskCase]:
        def _register(
            run: Callable[[CaseContext], Awaitable[str]],
        ) -> TaskCase:
            case = TaskCase(
                name=name,
                category=category,
                run=run,
                expect_failure=expect_failure,
            )
            self._cases.append(case)
            return case

        return _register

    @property
    def cases(self) -> list[TaskCase]:
        return list(self._cases)

    def add_case(self, case: TaskCase) -> None:
        """Register an already-built TaskCase (e.g. to run one on its own)."""
        self._cases.append(case)

    async def run(
        self, make_ctx: Callable[[], CaseContext] | None = None
    ) -> ScorecardReport:
        report = ScorecardReport()
        for case in self._cases:
            ctx = make_ctx() if make_ctx else CaseContext()
            started = time.monotonic()
            try:
                detail = await case.run(ctx)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                ok = case.expect_failure
                if not ok:
                    detail = f"FAILED: {detail}"
            else:
                ok = not case.expect_failure
                if case.expect_failure:
                    detail = f"UNEXPECTED SUCCESS: {detail}"
            seconds = time.monotonic() - started
            report.results.append(
                CaseResult(
                    name=case.name,
                    category=case.category,
                    ok=ok,
                    detail=detail,
                    seconds=seconds,
                    cost_usd=ctx.cost_usd,
                    faults_fired=ctx.faults_fired,
                    recovered=ok and ctx.faults_fired > 0,
                )
            )
        return report


def format_report(report: ScorecardReport) -> str:
    """Render the printable scorecard."""
    lines = ["Yaadhamma task scorecard", "=" * 60]
    for row in report.results:
        status = "PASS" if row.ok else "FAIL"
        fault = f", recovered from {row.faults_fired} fault(s)" if row.recovered else ""
        lines.append(
            f"{status} [{row.category}] {row.name} "
            f"({row.seconds:.2f}s, ${row.cost_usd:.4f}{fault})"
        )
        lines.append(f"      {row.detail}")
    lines.append("-" * 60)
    lines.append(
        f"{report.passed}/{report.total} passed "
        f"(pass rate {report.pass_rate:.0%}) · "
        f"recovery {report.recovery_rate:.0%} over "
        f"{len(report.fault_cases)} fault case(s) · "
        f"total {report.total_seconds:.1f}s · cost ${report.total_cost_usd:.4f}"
    )
    _go, reason = report.verdict()
    lines.append(reason)
    lines.append(
        "Costs are simulated (fakes report tokens, priced with "
        "costs.PRICE_PER_1K); no real-world side effects occurred."
    )
    return "\n".join(lines)
