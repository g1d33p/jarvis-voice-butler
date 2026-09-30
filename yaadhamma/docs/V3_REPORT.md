# Yaadhamma v3 build report

Branch `v3` (cut from `v2` tip `4f9057b`). One section per stage, newest
last. Each stage is finished, tested and committed before the next begins.
Push target: `v3` only. Nothing here touches `main`, `gemini-live`,
`hardening`, `v1` or `v2`.

Jeevan's standing rules for this build: no real-world side effects while
building (no WhatsApp sends, no emails, no calendar changes, no label
changes, no file moves, no paid model calls with his keys); diagnose
before fixing anything unrelated; every new behaviour gets success and
failure tests; British English.

---

## Stage 1 — One long-lived, invisible WhatsApp poller (built, sandbox-tested)

### What was built
- `scripts/remote_poller.py` (new): one resident process instead of a fresh
  browser launch every two minutes. It opens a single WhatsApp browser at
  startup and reuses it across all polls.
- `src/remote.py`: `ResidentPoller` — the resident loop. The digest lock is
  held for the browser's lifetime; when a digest requests the browser, the
  poller parks its browser, releases the lock, and reopens afterwards.
  Stale or dead request markers are ignored, and clearing a request can
  never erase another live process's request (overlapping-digest fix).
- Lost pairing is loud: the browser is parked, the profile freed, one
  health failure recorded per outage, a 15-minute wait, and the outage
  appears in status and the morning brief.
- The digest and the phone poller refuse a partial chat list *before* any
  email, calendar, model, send, or command execution.
- The digest browser never raises its window (non-raising by profile path);
  all focus paths go through `_maybe_bring_to_front()`.
- Chat-list settling: poll the cheap rendered count until it is unchanged
  twice or 20 seconds pass, then run one complete `list_all_chats()` scroll,
  then apply the persisted historical-best 50% guard — retry once, refuse if
  still too low. A test proves `18 → 60 → 135 → 135 → 135` settles into a
  complete 135-chat listing.

### Validation
- Focused suites (`test_remote.py`, `test_poller.py`, `test_digest.py`,
  `test_whatsapp_health.py`, `test_calendar.py`): 106 passed.
- `ruff check src scripts tests`: all checks passed.
- `ruff format --check`: 116 files already formatted.
- Remaining failures/errors were reproduced on the untouched `v2` base:
  missing Playwright Chromium / browser launch errors in the sandbox, plus
  `tests/test_step1i.py::test_quoted_message_becomes_replying_to` and
  `tests/test_step1g.py::test_direction_follows_bubble_position`.
- Credential/personal-data diff scan: no suspicious additions.

### Uncertainties (Stage 1)
- The 50% historical-best guard is a judgement call, not a measured
  threshold: real partial loads in testing showed small fractions of the
  list, so 50% catches them while tolerating normal fluctuation. If he
  legitimately archives a large fraction of his chats, the guard will
  refuse polls until the persisted baseline relearns — the refusal is loud
  (status + morning brief), never silent.
- Whether the chat count truly "settles" on real WhatsApp Web, and how
  long that takes on his account, is unverified in the sandbox (see
  "Sandbox limitations" below).

---

## Stage 2 — Prefix-free answers, 24/7 polling, one log line per poll (built, sandbox-tested)

### What was built
- **Prefix-free answers** (`src/remote.py`): when a pending question exists
  in one of his own chats, his next outgoing message there is the answer —
  no `Yaadhamma` prefix needed, any wording accepted ("yes", "no", "the
  second one"). The prefix is still mandatory for starting a *new* command;
  a new prefixed command replaces the waiting question (newest wins, as in
  the voice loop). A bare "yes" with no pending question does nothing.
- **30-minute expiry**: each stored question carries `asked_at`; an
  unanswered question older than 30 minutes is dropped, and a late reply
  must not resolve a question he has forgotten about. Records written by
  older versions (no timestamp) are treated as expired.
- **24/7 polling**: `YAADHAMMA_REMOTE_HOURS` now defaults to `0-24`
  (`src/config.py`). Setting the variable still restricts the window.
- **One summary line per poll**: `poll_with_client()` emits exactly one
  timestamped line on every path (normal, skipped, disabled, raising):
  `remote poll: chats_seen=N self_chats_matched=N commands_found=N
  actions_taken=N`. The old per-outcome poller lines are gone, so a quiet
  log means a quiet poll and one line per poll, no more.
- **Log rotation** (`scripts/remote_poller.py`): the old untimestamped
  `remote.log` is copied to `remote.log.1` and truncated exactly once on
  the resident poller's first start (marker-guarded, so launchd KeepAlive
  restarts never rotate again). Copy-then-truncate rather than rename:
  launchd opens the log path once at job spawn and keeps the descriptor in
  append mode, so renaming would have sent that generation's output to the
  renamed file. (Fixed after the initial commit; see commit `874d93c`.)

### Validation
- New tests: arbitrary answer resolves pending; bare "yes" without pending
  does nothing; expiry at 30 minutes (31 min ignored, 29 min still
  resolves); exactly one summary line per poll; 3 am poll runs by default;
  log rotation once-only (copy+truncate semantics).
- One existing Stage 1 test updated: `test_parse_hours_defaults_and_typos`
  now expects the all-day default.
- Affected suites: 114 passed. `ruff check`: all passed. `ruff format`:
  116 files formatted.

### Uncertainties (Stage 2)
- "His next outgoing message in this chat is the answer" depends on
  WhatsApp Web labelling the message direction correctly. Only his own
  chats are polled, so every message there should be his — but if the page
  ever mislabels direction, a non-answer could resolve a question. The
  30-minute expiry bounds the damage.
- The copy+truncate rotation assumes launchd opens `StandardOutPath` in
  append mode (it does — logs demonstrably append across restarts). If
  that ever changed, the first generation's output could land in a sparse
  region of the file; the marker still prevents repeats.

---

## Stage 3 — Two-pass email triage with deep read (built, sandbox-tested)

### What was built
- **Pass 1** (`classify_email` in `src/digest.py`): deterministic heuristics
  over sender, subject and preview only — no model call, so no cost and no
  prompt-injection surface. Labels: `noise`, `needs_reading`,
  `clearly_needs_him`. It considers: named vs bulk greeting ("hi jeevan"
  vs noreply@/newsletter), direct requests/questions, dates/deadlines/
  meetings/interviews, person vs `noreply@`, thread-participation hints
  ("following up", "as you mentioned"), and marketing boilerplate
  ("unsubscribe", "% off", …). A bulk job alert is `noise` however relevant
  the keywords; a named direct question from a person is
  `clearly_needs_him`. Classification happens on the raw values inside
  `collect_email`, before wrapping; only the label (our own token) reaches
  the model.
- **Pass 2** (`triage_deep_read`): fetches full bodies via `get_message`
  (read-only — Gmail state is never changed) for at most
  `YAADHAMMA_EMAIL_DEEP_READ` emails (default 8, `src/config.py`) from the
  latter two groups, clearest first. Bodies are wrapped with
  `untrusted.wrap` before model use. One model call returns per-email
  verdicts as JSON — `needs_you` plus a one-line `why` — parsed
  defensively: a broken reply or unknown ids mark nothing. Bodies over
  20,000 chars are truncated before the model sees them. Cost is recorded
  under feature `email_triage` in the local cost database.
- **Only pass 2 may assign *Needs you***: the digest instructions now put
  an email under *Needs you* only when `needs_you` is true, using its `why`
  for the reason. A `noise` email is still listable under *Email* from its
  snippet (so the existing "its own line for account/security/billing
  notices" behaviour is preserved) but can never reach *Needs you*.
- `collect_email` now also returns each message's `id` and pass-1 `triage`
  label (plain strings — safe for the brain payload).

### Validation
- New tests (10): marketing-job false positive (`noise`); direct recruiter
  question (`clearly_needs_him` → deep-read → `needs_you` with reason, cost
  recorded under `email_triage`); `noreply@` exclusion (never deep-read, no
  model call); ambiguous question → `needs_reading`; deep-read cap honoured
  (6 candidates, cap 3 → 3 fetches); body prompt-injection resistance (an
  "ignore previous instructions" body reaches the model only inside the
  untrusted envelope — verified by stripping envelope segments and
  asserting the injection appears nowhere outside one); broken model reply
  marks nothing; unknown verdict ids are dropped.
- Affected suites (`test_remote`, `test_poller`, `test_digest`,
  `test_whatsapp_health`, `test_calendar`, `test_costs`): 141 passed.
- `ruff check`: all passed. `ruff format`: 116 files formatted.

### Uncertainties (Stage 3)
- Pass-1 heuristics are English-centric and conservative by design. An
  automated security/billing alert from a `noreply@` address with no
  question or deadline in the preview is classified `noise`: it will still
  appear under *Email* from its snippet, but it will never reach *Needs
  you*. If Jeevan wants noreply@ security alerts escalated, that is a
  product call for him.
- Thread participation is heuristic only ("following up", …). True
  thread-participation detection would need thread metadata the triage does
  not fetch.
- Emails past the deep-read cap never get `needs_you`, even if pass 1
  marked them `clearly_needs_him`. The cap (default 8) is a cost control;
  raise `YAADHAMMA_EMAIL_DEEP_READ` if digests routinely carry more real
  candidates.
- Pass 2's verdicts depend on the model reading carefully; the JSON contract
  is enforced only by parsing defensively, not by schema validation.

---

## Sandbox limitations (all stages)

- **No real WhatsApp**: the sandbox has no Playwright Chromium, so no
  WhatsApp Web session exists here. Everything browser-shaped ran against
  fakes. The chat-count settling sequence (`18 → 60 → 135 → …`), the
  historical-best guard, the non-raising browser, and the lost-pairing path
  are proven against scripted fakes only.
- **Headless/off-screen behaviour unverifiable here**: the code sets
  headless mode and never brings the window forward, but whether the
  poller's Chromium truly stays invisible on macOS (no Dock icon, no
  window flash on poll) can only be proven on Jeevan's Mac. This is the
  single biggest "trust but verify on the Mac" item in v3.
- **No paid model calls during the build**: pass-2 triage's model call ran
  only against `FakeBrain`. The JSON verdict contract against the real
  brain is untested; the defensive parsing is the safety net.
- **No real Gmail**: `get_message` deep reads ran against fakes. Real
  body shapes (very long threads, attachments-only mails) are untested.

## Pre-existing v2 failures/errors left untouched (not authorised for v3)

Reproduced on the untouched `v2` base; deliberately not fixed:
- Missing Playwright Chromium / browser launch errors in the sandbox
  (environment limitation, not a code defect).
- `tests/test_step1i.py::test_quoted_message_becomes_replying_to`
- `tests/test_step1g.py::test_direction_follows_bubble_position`

Also not touched, per standing instructions: the unreachable block after
`format_session_state()` in `scripts/daemon_control.py`, and the
Mac-specific v1 openWakeWord crash.

## Required Mac actions

1. **Prune stale linked devices** — on the phone: WhatsApp → Settings →
   Linked devices, and remove any entries Jeevan does not recognise. Rationale:
   WhatsApp evicts the least-recently-used linked device when the limit is
   hit, which can silently unpair the digest/poller profile. Fewer stale
   entries = fewer surprise unpairings. (The poller reports lost pairing
   loudly when it happens anyway.)
2. Check out `v3`, `uv sync`, run the self-test, and confirm the resident
   poller stays invisible: `uv run scripts/remote_schedule.py on`, then
   watch for any Chromium window or Dock icon appearing during polls.
3. `tail -f ~/.yaadhamma/remote.log` — expect exactly one
   `remote poll: chats_seen=…` line per poll, timestamped.
4. Run one digest and check an email that needs him lands under *Needs
   you* with a reason, and that marketing mail does not.
5. If Jeevan wants a different polling window: `YAADHAMMA_REMOTE_HOURS`
   in `.env.local` (e.g. `8-23`); if digests routinely carry more than 8
   real email candidates: `YAADHAMMA_EMAIL_DEEP_READ` in `.env.local`.

## Chat-count settling: design and deliberation

**Problem**: WhatsApp Web renders the chat list lazily. Reading it too
early yields a fraction of the chats, and acting on a fraction as if it
were all of them is the worst failure mode (missed commands, partial
digests presented as complete).

**Design** (Stage 1): (1) poll the cheap rendered count until it is
unchanged twice or 20 s elapse; (2) run one complete `list_all_chats()`
scroll; (3) compare against the persisted historical best — if the count
is under 50% of the best, retry once, then refuse loudly rather than act.

**Deliberation**: the 50% threshold is a judgement call. Observed partial
loads were small fractions (tens of chats against a ~135 baseline), so
50% separates "still loading" from "normal fluctuation" with margin. A
lower threshold would risk acting on partial lists; a higher one would
refuse polls after legitimate mass-archiving. Refusal is always loud
(status + morning brief + `remote.log`), never silent, and the baseline
relearns from successful full reads — so a legitimately shrunken list
recovers on its own after loud refusals, which is the honest behaviour:
Jeevan should know his poller thinks half his chats vanished.

## Uncertainties register (everything not provable in the sandbox)

1. Real headless/invisible Chromium behaviour on macOS — verify on the Mac.
2. True chat-list settle time on his account and connection.
3. The 50% guard threshold against his real chat-count variance.
4. Pass-2 verdict quality against the real brain (JSON contract untested
   live; defensive parsing is the net).
5. Pass-1 heuristic misses: non-English mail, noreply@ security alerts,
   sarcastic/ironic requests.
6. Pending-answer direction labelling on real WhatsApp Web (bounded by
   the 30-minute expiry).
7. Whether Gmail `get_message` bodies for very long threads stay sane
   (truncated at 20,000 chars as a guard).
