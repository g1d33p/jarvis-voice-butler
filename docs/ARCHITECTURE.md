# Sureedu architecture (Phase 3)

```
 You (voice) ──► Gemini Live (voice model)           Mac + Sureedu's browser
                  │  quick actions: open site/tab,         ▲
                  │  switch/close tabs, observe, apps,     │
                  │  screenshot, clipboard, open folder ───┤
                  │                                        │
                  └─ run_task(goal) ─► Orchestrator ───────┘
                     continue_task       (text model loop:
                                          think → tool → result → repeat)
                                              │
                                              ▼
                                      Task store (SQLite, ~/.sureedu/sureedu.db)
```

## Files

| File | Role |
|---|---|
| `src/agent.py` | Starts the voice session; chooses split or direct mode. |
| `src/config.py` | Settings from `.env.local` (mode, models, limits). |
| `src/prompts.py` | `VOICE_INSTRUCTIONS` (short), `ORCHESTRATOR_INSTRUCTIONS`, and the old `AGENT_INSTRUCTIONS` for direct mode. |
| `src/orchestrator.py` | The background loop, voice task tools, which tools the voice keeps. |
| `src/actions.py` | Registry that lets the orchestrator describe and call every tool. |
| `src/task_manager.py` | `Task` records and the SQLite store. |
| `src/tools.py`, `src/browser.py` | Browser control, approval gate for sends. |
| `src/policy.py` | Which messages may be sent without asking. |
| `src/mac_tools.py`, `src/file_tools.py` | Mac apps, clipboard, screenshots, files, Trash. |
| `src/observation.py` | `observe_state`: front app, window, tabs, what changed. |

## Safety model

The language models propose; code decides. Sends and other consequential clicks
go through one gate in `tools.py`, shared by the voice model and the
orchestrator. It allows a send only when policy allows it (short, dictated,
non-sensitive) or when the real speech transcript shows a clear yes to that
exact action. Nothing a model writes can approve an action by itself.

## Settings (`sureedu/.env.local`)

| Setting | Default | Meaning |
|---|---|---|
| `SUREEDU_MODE` | `split` | `direct` restores the Phase 1–2 behaviour. |
| `SUREEDU_BRAIN_MODEL` | `gemini-3.5-flash-lite` | Background model for tasks. |
| `SUREEDU_ESCALATION_MODEL` | `gemini-3.6-flash` | Used after two failed steps in a row. |
| `SUREEDU_MAX_TASK_STEPS` | `15` | Tool rounds per task. |
| `SUREEDU_TASK_TIMEOUT_SECONDS` | `120` | Time limit per task. |
