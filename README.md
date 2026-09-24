# Sureedu — Voice-Driven Personal Assistant for macOS

Sureedu is a local-first, voice-driven AI personal assistant for macOS, built on the
**LiveKit Agents** framework. Speak to Sureedu and it replies as a concise,
slightly sarcastic British-English butler while controlling a real browser,
native Mac applications, and your files — asking for approval before anything
consequential.

Sureedu began as a customization of an open-source voice-butler starter project.

## Current capabilities

- **Voice conversation** — real-time voice via LiveKit and Google Gemini Live
  (`gemini-3.1-flash-live-preview`, British English voice). Understands English,
  Telugu, and mixed speech; replies in English.
- **Browser control** — a dedicated, persistent Playwright Chromium profile
  (`~/.sureedu/chrome-profile`) that keeps logged-in sessions such as WhatsApp Web.
  Tools: `open_url`, `search_the_web`, `read_page`, `inspect_page`, `go_back`,
  `take_screenshot`, `click`, `type_text`, `scroll`, `press_key`,
  `confirm_browser_action`.
- **macOS application control** — `list_running_apps`, `open_application`,
  `quit_application`.
- **Filesystem control** — `get_home_directory`, `list_directory`, `search_files`,
  `inspect_path`, `create_folder`, `create_file`, `rename_path`, `move_path`,
  `copy_path`. There is deliberately no permanent-delete tool.
- **Clients** — console mode, a Next.js web frontend, and a Flutter app.

Audio only: camera and screen-share input are disabled.

## Project structure

```
jarvis-voice-butler/
├── jarvis_new/                 # Main project
│   ├── src/
│   │   ├── agent.py            # Entrypoint — AgentServer, Assistant class
│   │   ├── browser.py          # BrowserManager (Playwright Chromium)
│   │   ├── tools.py            # Browser function tools
│   │   ├── mac_tools.py        # macOS application tools
│   │   ├── file_tools.py       # Filesystem tools
│   │   └── prompts.py          # Sureedu instructions and personality
│   ├── tests/
│   ├── frontend/               # Next.js web UI
│   └── pyproject.toml
└── agent-starter-flutter/      # Flutter client
```

## Setup

Prerequisites: Python ≥ 3.10, [uv](https://docs.astral.sh/uv/), Node.js ≥ 20, pnpm.

```bash
cd jarvis_new
uv sync
uv run playwright install chromium
cp .env.example .env.local          # then fill in credentials
```

Required in `jarvis_new/.env.local`: `LIVEKIT_URL`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, `GOOGLE_API_KEY`.

The web frontend needs the same LiveKit values plus `AGENT_NAME=sureedu` in
`jarvis_new/frontend/.env.local`.

## Running

```bash
cd jarvis_new
uv run src/agent.py console     # terminal voice chat
uv run src/agent.py dev         # for the web frontend
```

Web frontend (start the agent first):

```bash
cd jarvis_new/frontend
pnpm install && pnpm dev        # http://localhost:3000
```

## Safety

- Never commit `.env.local`, API keys, or `~/.sureedu/` (browser profile, cookies).
- Consequential actions (sending, deleting, submitting, purchasing) require
  explicit user approval.
- Sureedu must not report success unless a tool confirms it.

## Roadmap

See the project roadmap: task orchestrator, verification and recovery, a central
permission manager, Outlook/WhatsApp integrations, personal memory, and a
background scheduler.
