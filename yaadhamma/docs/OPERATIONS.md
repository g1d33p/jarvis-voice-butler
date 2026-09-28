# Yaadhamma Operations

Day-to-day running of the assistant on Jeevan's Mac. Everything here
assumes the project checked out at `~/jarvis-voice-butler` and Python
managed with `uv`.

## Quick health check

```bash
cd ~/jarvis-voice-butler/yaadhamma
uv run scripts/status.py
```

One screen: last 7 days of model spend per feature, month-to-date
against the budget, WhatsApp pairing, Gmail/Calendar sign-in, disk
space, and recent errors from the audit log. Read-only; changes nothing.

## Starting and stopping

- Voice agent: run `uv run src/agent.py` in a terminal (foreground, so
  approval prompts reach Jeevan).
- Scheduled jobs: installed with `uv run scripts/digest_schedule.py on`,
  removed with `... off`. They run headless via launchd; logs land in
  `~/.yaadhamma/logs/`.

## Sign-in and pairing

| Service | Script | Notes |
|---|---|---|
| WhatsApp | `scripts/whatsapp_signin.py` | Stop the agent first (the Chromium profile is single-window), then scan the QR with the phone |
| Gmail | `scripts/gmail_signin.py` | One token per account label, owner-only permissions |
| Calendar | `scripts/calendar_signin.py` | Google OAuth, same pattern as Gmail |

Tokens live under `~/.yaadhamma/` and are never committed.

## Approvals

Consequential actions (send a message or email, create a calendar
event, trash a file) pause for Jeevan's explicit approval in the
terminal or voice session. One approval = one action, expires after 60
seconds. Every approval, refusal and verification outcome is appended to
`~/.yaadhamma/audit.jsonl`.

## Budget

`YAADHAMMA_MONTHLY_BUDGET_USD` in `.env.local` (default 20). The morning
brief prints a warning when the month's estimated spend crosses it. It
never blocks anything — the provider billing page is the only real
meter. If Gemini calls start failing with 429s, check the AI Studio
spending cap first.

## Logs and databases

- `~/.yaadhamma/logs/` — launchd job output
- `~/.yaadhamma/audit.jsonl` — approvals and verification results
- `~/.yaadhamma/yaadhamma.db` — tasks, `model_calls` cost table,
  WhatsApp health history
- `~/.yaadhamma/memory.db` — durable memories
- `~/.yaadhamma/voice_metrics.csv` — per-reply latency and tokens

## When something breaks

1. Run `scripts/status.py` — it surfaces the last 24h of audit errors.
2. Check `~/.yaadhamma/logs/` for the failing job.
3. WhatsApp Web: run `scripts/whatsapp_read_check.py` (reads, sends
   nothing) or the full `digest_run.py --check`.
4. Working rule: diagnose and explain the causes and options, get
   Jeevan's go-ahead, then fix. Never jump straight to fixing.

## Updating the code

The Mac is a read-only mirror of GitHub: `git fetch origin &&
git reset --hard origin/<branch>`. Never commit on the Mac or the
histories diverge. Changes are built and pushed from the dev machine,
then pulled here.
