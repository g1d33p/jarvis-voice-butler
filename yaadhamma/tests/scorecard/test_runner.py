"""Tests for the scorecard runner mechanics (src/scorecard.py)."""

import pytest

from scorecard import Scorecard, format_report


class _FakeWithFaults:
    """Minimal fake: the runner only needs a faults_fired counter."""

    def __init__(self, faults_fired: int = 0) -> None:
        self.faults_fired = faults_fired


async def test_passing_case_records_success() -> None:
    card = Scorecard()

    @card.case("demo pass", category="demo")
    async def _run(ctx):
        return "did the thing"

    report = await card.run()
    assert report.total == 1
    assert report.passed == 1
    row = report.results[0]
    assert row.ok is True
    assert row.detail == "did the thing"
    assert row.seconds >= 0
    assert row.recovered is False


async def test_failing_case_records_failure_with_reason() -> None:
    card = Scorecard()

    @card.case("demo fail", category="demo")
    async def _run(ctx):
        raise RuntimeError("boom")

    report = await card.run()
    assert report.passed == 0
    row = report.results[0]
    assert row.ok is False
    assert "boom" in row.detail


async def test_expected_failure_passes_when_it_fails_gracefully() -> None:
    """Some tasks should fail (unknown contact): the pass is the graceful no."""
    card = Scorecard()

    @card.case("no such chat", category="demo", expect_failure=True)
    async def _run(ctx):
        raise RuntimeError("No WhatsApp chat named 'Nobody' found.")

    report = await card.run()
    assert report.passed == 1
    assert report.results[0].ok is True


async def test_unexpected_success_fails_an_expect_failure_case() -> None:
    card = Scorecard()

    @card.case("should have failed", category="demo", expect_failure=True)
    async def _run(ctx):
        return "sent it anyway"

    report = await card.run()
    assert report.passed == 0


async def test_recovery_detected_when_fault_fires_then_case_passes() -> None:
    card = Scorecard()

    @card.case("flaky then fine", category="demo")
    async def _run(ctx):
        ctx.watch(_FakeWithFaults(faults_fired=2))
        return "recovered"

    report = await card.run()
    row = report.results[0]
    assert row.ok is True
    assert row.faults_fired == 2
    assert row.recovered is True
    assert report.recovery_rate == pytest.approx(1.0)


async def test_no_recovery_without_faults() -> None:
    card = Scorecard()

    @card.case("clean pass", category="demo")
    async def _run(ctx):
        ctx.watch(_FakeWithFaults())
        return "fine"

    report = await card.run()
    assert report.results[0].recovered is False
    # No fault-bearing cases: recovery is vacuously perfect, not a failure.
    assert report.recovery_rate == pytest.approx(1.0)


async def test_cost_accumulates_across_cases() -> None:
    card = Scorecard()

    @card.case("costly", category="demo")
    async def _run(ctx):
        ctx.note_cost(0.001, "simulated brain turn")
        ctx.note_cost(0.002, "simulated voice reply")
        return "done"

    @card.case("free", category="demo")
    async def _run(ctx):
        return "done"

    report = await card.run()
    assert report.total_cost_usd == pytest.approx(0.003)


async def test_verdict_needs_ninety_percent_pass() -> None:
    card = Scorecard()
    for i in range(9):

        @card.case(f"pass {i}", category="demo")
        async def _run(ctx):
            return "ok"

    @card.case("the failure", category="demo")
    async def _fail(ctx):
        raise RuntimeError("nope")

    report = await card.run()
    assert report.pass_rate == pytest.approx(0.9)
    go, reason = report.verdict(min_pass_rate=0.9, min_recovery_rate=1.0)
    assert go is True, reason

    @card.case("one more failure", category="demo")
    async def _fail2(ctx):
        raise RuntimeError("nope again")

    report = await card.run()
    go, reason = report.verdict(min_pass_rate=0.9, min_recovery_rate=1.0)
    assert go is False
    assert "pass rate" in reason


async def test_verdict_requires_recovery_proven() -> None:
    card = Scorecard()

    @card.case("flaky but lost", category="demo")
    async def _run(ctx):
        ctx.watch(_FakeWithFaults(faults_fired=1))
        raise RuntimeError("never recovered")

    report = await card.run()
    assert report.recovery_rate == pytest.approx(0.0)
    go, reason = report.verdict(min_pass_rate=0.0, min_recovery_rate=1.0)
    assert go is False
    assert "recovery" in reason


async def test_format_report_prints_the_card() -> None:
    card = Scorecard()

    @card.case("summarise inbox", category="gmail")
    async def _run(ctx):
        ctx.note_cost(0.0001, "simulated")
        return "3 unread summarised"

    report = await card.run()
    text = format_report(report)
    assert "summarise inbox" in text
    assert "gmail" in text
    assert "PASS" in text
    assert "pass rate" in text
    assert "recovery" in text
    assert "cost" in text
