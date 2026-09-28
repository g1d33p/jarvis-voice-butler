# Yaadhamma v1 Build Report

Built 2026-09-28 on branch `v1`, cut from the `hardening` base
`7447f4f6d855`. No real-world side effects at any point: all tests use
fakes and `tmp_path`; no messages or email were sent, no calendar or
Gmail changes were made, no paid models were called with Jeevan's keys,
and no real files were touched outside temporary test directories.

## Stages and commits (one per stage)

| Stage | Commit | What |
|---|---|---|
| 0 | `3461447` | Fix hardening review defects (outside-content envelope, Gmail send verification, Gemini pricing/cost streams, non-fatal cost storage, USD 35 budget default) |
| 1 | `b31c6e0` | Delete Outlook and Meta/Muse pipeline dead weight; `src/agent.py` and Gemini Live preserved |
| 2 | `1e6db4c` | Fake-client task scorecard with release thresholds |
| 3 | `7eeacaf` | Wake word ("Hey Jarvis") and always-on launchd daemon |
| 4 | `27df936` | Menu-bar UI and floating orb (optional, never fatal) |
| 5 | `2329a05` | Phone access over WhatsApp (remote commands; orchestrator wired with its registry/store, durable approvals) |
| 6 | `1aa584d` | Gmail labels and guarded archiving |
| 7 | `093d3cc` | Commitment due dates, reminders, explicit completion |
| 8 | `772b0b0` | File tidy defaults to apply; guards preserved |
| 9 | _(this commit)_ | Selftest, settings reference, docs; month-day parser review fix |

## Verification (final run, 2026-09-28)

- **pytest**: 555 passed across `tests/` (scorecard directory run
  standalone; `tests/test_browser.py` excluded — see below).
- **Ruff**: `ruff check` and `ruff format --check` clean across
  `src/`, `scripts/`, `tests/`.
- **selftest**: `uv run scripts/selftest.py` — 9/9 checks pass. Each check
  exercises real code paths (due-date parser, commitment round-trip,
  explicit-only completion, email classification + archive guards, Gmail
  scope detection, file-tidy apply guards, remote command detection) and
  verifies `docs/SETTINGS.md` documents every `YAADHAMMA_*` variable the
  code reads.
- **Scorecard**: `uv run scripts/scorecard.py` — 18/18 tasks pass, 100%
  pass rate and 100% fault recovery against the release thresholds,
  simulated cost $0.0043. The fake orchestrator recovers from injected
  tool failures without human help.
- **Honest exclusions** (environment limits of the sandbox, not code):
  `tests/test_browser.py` hangs here (no Chromium display/browser
  executable); 2 end-to-end orchestrator tests, 1 step1g test and 1
  whatsapp-mouse test error at setup because the Playwright browser
  executable is not installed; 3 `test_agent.py` tests need a real
  `api_key`. None were hidden or deleted; all are Mac-acceptance items.

## Review fixes folded into Stage 9

- **Month-day clamp (Stage 7 review item)**: `parse_due_date()` clamped
  every day above 28 to 28, so "Jan 31" silently became Jan 28. It now
  clamps only to the month's real last day ("Jan 31" stays Jan 31;
  "Feb 30" becomes Feb 28/29). Covered by two regression tests in
  `tests/test_commitments.py`.
- **Stale budget default in docs**: OPERATIONS.md and ARCHITECTURE.md said
  default 20; the code default (set in Stage 0) is USD 35. Corrected.
- **selftest found on its first run**: SETTINGS.md was missing two context
  variables the code reads (now documented); a test-helper bug in the
  selftest itself (wrong dict key) was fixed.

## Open questions (per the stop-on-uncertainty rule)

These can only be answered on Jeevan's Mac; they are not guesses.

1. **Microphone and voice loop**: "Hey Jarvis" detection, the 90-second
   silence timeout, Option+Space, and the Sulafat voice are real-hardware
   behaviours — unverified in the sandbox.
2. **Menu-bar UI rendering**: the state mapping and menu actions are
   unit-tested; actual rendering needs macOS.
3. **WhatsApp pairing**: `scripts/whatsapp_signin.py` QR pairing and real
   send/read are unverified in the sandbox (the remote poll's parsing,
   allowlist, dedupe, approvals and hours window are covered by fakes).
4. **Gmail/Calendar sign-in**: the OAuth flows and the `gmail.modify`
   re-link need Jeevan's browser; `gmail_signin.py --check` reports what
   is missing.
5. **Google AI Studio spending cap**: on 2026-09-24 the Gemini backup died
   on a monthly spend-cap 429. If Gemini calls start failing with 429s,
   check the AI Studio spending cap first (noted in OPERATIONS.md).

## Known quirks (not hidden, not blockers)

- **Scorecard collection layout**: running `pytest tests/` *together with*
  `tests/scorecard/` fails at collection because
  `tests/scorecard/test_cases.py` imports a top-level `cases` module.
  This predates v1; the scorecard runs green standalone and via its
  script. Left as-is rather than restructuring the scorecard at the end
  of the build.
- **Chromium-dependent tests**: anything driving real WhatsApp Web cannot
  run in the sandbox; those paths are covered by fakes and reported as
  Mac acceptance above.
- **Due dates are naive local datetimes**, consistent with the rest of the
  Mac-local scheduling design (launchd jobs, `America/Chicago` default).

## Hygiene

- `grep -ri sureedu` over the tree: no matches.
- No credentials, tokens or message contents in source control (`.env.local`
  gitignored; Secure Vault for secrets).
- `src/agent.py` preserved through every stage.
- Push authority covers `v1` only; `main`, `hardening`, `gemini-live` and
  `phase-1-meta-brain` were not touched.
