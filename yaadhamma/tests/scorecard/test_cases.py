"""Each scorecard task runs as its own pytest test against the fakes."""

import pytest
from cases import register
from fakes import Context  # noqa: F401  (kept importable for debugging)

from scorecard import CaseContext, Scorecard, ScorecardReport


def _all_cases():
    card = Scorecard()
    register(card)
    return card.cases


_CASES = _all_cases()
assert len(_CASES) >= 15, f"scorecard needs 15-20 tasks, has {len(_CASES)}"


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.name)
async def test_scorecard_case(case, tmp_path) -> None:
    """Run one task; the runner's verdict logic decides pass/fail."""
    single = Scorecard()
    single.add_case(case)

    def make_ctx() -> CaseContext:
        ctx = CaseContext()
        ctx.tmp_path = tmp_path  # type: ignore[attr-defined]
        return ctx

    report: ScorecardReport = await single.run(make_ctx=make_ctx)
    assert len(report.results) == 1
    row = report.results[0]
    assert row.ok, f"{case.name}: {row.detail}"
