# Hardening Handoff Report

Overnight build, 2026-09-28. Branch `hardening`, cut from
`origin/gemini-live` at `da331c0` (Step 8: WhatsApp self-check). Three
commits, all on `hardening`; `gemini-live`, `main` and
`phase-1-meta-brain` untouched.

No real-world side effects: no messages or emails sent, no calendar
events created, no files moved on the Mac, no live account or model
calls. All tests use fake clients and throwaway directories.

## Stage 1 — Untrusted-content boundary (committed `bd72fbd`)

`src/untrusted.py`: every piece of outside-world text (email, WhatsApp,
calendar, web pages, interview research) is wrapped in
`<<UNTRUSTED_CONTENT source="...">>` markers before a model sees it, and
every instruction block tells the model wrapped text is data to report
on, never instructions to follow. 73 tests in `tests/test_untrusted.py`.

Honest limit: the tests prove the wrapping is structural and the
approval gates still fire. They do not prove a real Gemini model will
always report an injection instead of obeying it — that needs a live
model, which was out of bounds for this build.

## Stage 2 — Verify consequential actions (committed `391051e`)

After file ops, memory writes, calendar creation, WhatsApp sends, email
sends and tidy moves, the agent re-checks reality (file really there,
memory reads back the same, event re-fetches, message appears in chat).
`verified: false` means "unconfirmed", never "definitely failed". The
audit log now records the verification outcome on every `action_done`
entry. 17 tests in `tests/test_verification.py`. Email/calendar
verification confirms API acceptance, not recipient delivery — said
plainly in the code and the tests.

## Stage 3 — Cost and health dashboard (committed)

- `src/costs.py`: every model call recorded with feature, model, tokens
  and estimated USD into `~/.yaadhamma/yaadhamma.db`. Recording can
  never break or block a call. Pricing in `PRICE_PER_1K` is a rough
  estimate — needs review against the AI Studio billing page.
- `YAADHAMMA_MONTHLY_BUDGET_USD` (default 20). The morning brief warns
  when crossed; it never blocks.
- `scripts/status.py`: one screen for 7-day spend per feature,
  month-to-date vs budget, WhatsApp pairing, Gmail/Calendar sign-in,
  disk space, recent audit errors. Read-only.
- 13 tests in `tests/test_costs.py`, plus a suite-wide conftest fixture
  that keeps cost records out of the real database during tests.

## Not done

- **Stage 4 (task scorecard)**: not attempted; Stages 1-3 took the
  night. The branch is better for one finished stage than several
  half-built ones.
- **Stage 5 (retire Meta pipeline)**: deliberately not started — the
  handoff says only after Stage 4 exists.
- **Stage 6 (wake word daemon)**: not started.
- **Full suite**: `pytest tests -k "not test_agent"` hangs in the
  sandbox on `tests/test_browser.py` (real Chromium/HTTP server, needs
  the Mac). Everything else runs; see the commit messages for counts.
  `ruff check` and `ruff format` are green.
- **Needs Jeevan's Mac**: WhatsApp pairing and the self-check, Gmail and
  Calendar sign-in, the morning brief and digest end-to-end, voice loop
  with Sulafat, and confirming the AI Studio spend-cap 429 is resolved.

## Questions for Jeevan

1. Are the model price estimates in `src/costs.py` close enough, or
   should I pull real numbers from the AI Studio billing page?
2. Is $20/month the right default budget?
3. Merge `hardening` into `gemini-live` now, or keep it separate until
   the Mac-side acceptance is done?
