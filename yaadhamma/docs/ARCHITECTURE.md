# Yaadhamma Architecture (Phase 1: Meta brain)

## Voice stack

Default (`YAADHAMMA_VOICE_MODE=pipeline`):

```text
Microphone -> Meta Voice Transcribe (STT) -> Muse Spark (LLM)
    -> LiveKit Inference (TTS, feminine voice) -> Speaker
```

All three stages are wired through LiveKit's `AgentSession` in `src/agent.py`
(`voice_components()`). The LLM is Muse Spark served through the Meta Model
API's OpenAI-compatible surface (`https://api.meta.ai/v1`,
`Authorization: Bearer <key>`).

Fallback (`YAADHAMMA_VOICE_MODE=realtime`, or pipeline requested but no
`YAADHAMMA_MODEL_API_KEY` set): the previous Gemini Live realtime path is used
instead, with a loud warning logged when the fallback is automatic. The
realtime path keeps its own server-side turn detection and interruption
handling.

## Background brain

`src/orchestrator.py` runs multi-step tasks through `MetaBrainClient`
(`src/meta_client.py`), a thin wrapper over OpenAI-compatible Chat Completions
with tool calling:

- Assistant messages carry parallel `tool_calls`; each result is posted back
  as a `role: "tool"` message keyed by `tool_call_id`.
- `reasoning_effort` is forwarded to the API (Muse Spark supports effort
  levels). After two consecutive tool failures the orchestrator escalates:
  it switches to `YAADHAMMA_ESCALATION_MODEL`, or — when the brain already is
  the escalation model — raises the reasoning effort to
  `YAADHAMMA_ESCALATION_EFFORT` (default `high`).
- Old tool results are shrunk once more than two large results accumulate,
  keeping the latest two whole.

## Speech recognition

`MetaRealtimeSTT` (`src/meta_client.py`) is a LiveKit `STT` plugin over the
Voice Transcribe realtime websocket (`wss://api.meta.ai/v1/asr/realtime`).
It sends a JSON start frame, then raw 16 kHz mono PCM, and converts server
transcript frames into LiveKit speech events (start-of-speech, interim,
final). One-shot transcription is available via `POST /v1/asr/transcribe`
(`MetaRealtimeSTT.recognize`).

> Status note: the realtime websocket frame schema is provisional — the
> official realtime protocol page could not be fetched during development.
> Verify it against the live Meta docs and a real key before first use.

## Privacy boundaries

- Memory, credentials, browser profile, and task history stay local.
- The Meta Model API receives only what voice/model operation needs:
  transcribed speech, conversation text, and tool definitions/results.
- No API keys are committed; `scripts/smoke_meta.py` reads the key from the
  environment only.

## Cost notes (per Meta's published pricing at implementation time)

- `muse-spark-1.3`: $1.25 / 1M input tokens, $4.25 / 1M output tokens.
- `muse-voice-transcribe-1.0`: $0.18 / hour of audio.

Re-check pricing in the Meta developer console before relying on it.
