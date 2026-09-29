import logging
from datetime import datetime

from dotenv import load_dotenv
from google.genai import types as genai_types
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    cli,
    room_io,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import ai_coustics, google

import config
from actions import ActionRegistry
from audit import AuditLog
from browser import BrowserManager
from calendar_tools import CalendarTools
from commitment_tools import CommitmentTools
from digest import DigestTools
from file_tools import FileTools
from gmail_tools import GmailTools
from latency import VoiceMetrics, note_task_running
from mac_tools import MacTools
from memory_tools import MemoryTools
from observation import ObservationTools
from orchestrator import Orchestrator, TaskTools, voice_tools
from permissions import ApprovalManager, ApprovalTools
from planner import PlanTools
from prompts import AGENT_INSTRUCTIONS, VOICE_INSTRUCTIONS
from task_manager import TaskStore
from tools import BrowserTools
from whatsapp_tools import WhatsAppTools

logger = logging.getLogger("yaadhamma")

load_dotenv(".env.local")  # also loaded by config; harmless twice


def _current_time_note() -> str:
    """Tell the model the local date and time, for greetings and scheduling."""
    now = datetime.now().astimezone()
    return (
        "\n\n# Current Time\n\n"
        f"When this conversation started it was {now:%A, %d %B %Y, %I:%M %p} "
        f"({now.tzname()}) on the user's Mac."
    )


def _commitments_note() -> str:
    """Overdue and due-today commitments, reviewed on every wake.

    Never fatal: if the store cannot be read, the session starts without it.
    """
    try:
        from task_store import wake_review_text

        review = wake_review_text()
    except Exception:
        return ""
    if not review:
        return ""
    return "\n\n# Commitments\n\n" + review


def voice_components():
    """Build the Gemini Live realtime voice LLM.

    Gemini Live (YAADHAMMA_REALTIME_MODEL, normally gemini-3.8-live) hears,
    thinks and speaks in one model, with the voice Jeevan chose. On 3.8 Live,
    tools run in the background by default, so she can keep talking while a
    task works; results are spoken once she is idle.
    """
    options = {
        "model": config.REALTIME_MODEL,
        "voice": config.REALTIME_VOICE,
        # Speak tool results when she is not mid-sentence.
        "tool_response_scheduling": genai_types.FunctionResponseScheduling.WHEN_IDLE,
        # Wait a little longer before deciding he has finished a sentence
        # (2026-09-25: "All right. Do you know" was cut off mid-question).
        "realtime_input_config": genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
                silence_duration_ms=config.END_OF_SPEECH_SILENCE_MS,
            )
        ),
        # Cost control: Gemini Live re-bills the whole conversation on every
        # reply, so keep only a recent window once it grows.
        "context_window_compression": genai_types.ContextWindowCompressionConfig(
            trigger_tokens=config.CONTEXT_TRIGGER_TOKENS,
            sliding_window=genai_types.SlidingWindow(
                target_tokens=config.CONTEXT_TARGET_TOKENS
            ),
        ),
    }
    if config.REALTIME_LANGUAGE:
        options["language"] = config.REALTIME_LANGUAGE
    try:
        return google.beta.realtime.RealtimeModel(**options)
    except Exception as exc:
        raise RuntimeError(
            "The Gemini Live voice could not start: check GOOGLE_API_KEY in .env.local."
        ) from exc


def _local_vad():
    """The small on-device voice detector, or None if switched off."""
    if not config.LOCAL_VAD:
        return None
    from livekit.agents import inference

    return inference.VAD(min_silence_duration=config.LOCAL_VAD_SILENCE_S)


def build_toolsets(browser: BrowserManager | None = None):
    """Every toolset, sharing one approval manager and audit log.

    Used by the voice assistant and by phone commands (remote.py), so both
    paths get identical tools, gates and audit entries.
    """
    browser = browser or BrowserManager(headless=True)
    audit_log = AuditLog()
    approvals = ApprovalManager(audit=audit_log)
    by_name = {
        "browser_tools": BrowserTools(browser, approvals=approvals),
        "mac_tools": MacTools(approvals=approvals),
        "file_tools": FileTools(approvals=approvals),
        "gmail_tools": GmailTools(approvals=approvals),
        "approval_tools": ApprovalTools(approvals=approvals),
        "observation_tools": ObservationTools(browser),
        "memory_tools": MemoryTools(audit=audit_log),
        "commitment_tools": CommitmentTools(),
        "whatsapp_tools": WhatsAppTools(browser=browser, approvals=approvals),
        "calendar_tools": CalendarTools(approvals=approvals),
    }
    shared = {"audit_log": audit_log, "approvals": approvals, "by_name": by_name}
    return tuple(by_name.values()), shared


class Assistant(Agent):
    def __init__(self, browser: BrowserManager | None = None) -> None:
        # The voice: Gemini Live speech-to-speech (realtime).
        self._voice_llm = voice_components()
        self.browser = browser or BrowserManager(headless=True)
        toolsets, shared = build_toolsets(self.browser)
        self.audit_log = shared["audit_log"]
        self.approvals = shared["approvals"]
        for name, toolset in shared["by_name"].items():
            setattr(self, name, toolset)
        self._end_call_tool = EndCallTool(
            extra_description=(
                "Only end the call after the user clearly says they are finished, "
                "says goodbye, or directly asks to end the call."
            ),
            end_instructions=(
                "Give Yaadhamma's brief, polite British-English farewell, then end the call."
            ),
        )
        if config.MODE == "direct":
            # Phase 1-2 behaviour: the voice model does everything itself.
            instructions = AGENT_INSTRUCTIONS
            tools = [tool for toolset in toolsets for tool in toolset.tools]
        else:
            # Phase 3 split: the voice model talks and does quick actions; a
            # cheaper background model carries out multi-step tasks.
            self.orchestrator = Orchestrator(
                registry=ActionRegistry(*toolsets), store=TaskStore()
            )
            instructions = VOICE_INSTRUCTIONS
            # note_task_running lets the daemon see background work, so the
            # push-to-talk idle timeout never closes a session mid-task.
            task_tools = TaskTools(self.orchestrator, activity_hook=note_task_running)
            tools = [
                *voice_tools(*toolsets),
                *task_tools.tools,
                *DigestTools().tools,
                *PlanTools().tools,
            ]

        super().__init__(
            # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
            # See all available models at https://docs.livekit.io/agents/models/llm/
            llm=self._voice_llm,
            instructions=instructions + _current_time_note() + _commitments_note(),
            tools=[*tools, *self._end_call_tool.tools],
        )


server = AgentServer()


def _start_ptt_mic_watcher(ctx: JobContext) -> None:
    """Push-to-talk mic gate, worker side (v2 Stage 1).

    The daemon writes ~/.yaadhamma/ptt-mic.json on key release (mic off) and
    key press (mic on); this thread applies it to the console's microphone
    input. Release stops the mic, never the session: a task in flight runs
    to completion and she speaks the answer. Polling (not signals) because
    the daemon's subprocess handle is the `uv run` parent, not this Python
    process. Never raises; a missing file means "mic on".
    """
    import threading

    from hotkey import read_ptt_mic_enabled

    stop = threading.Event()

    def _watch() -> None:
        last: bool | None = None
        while not stop.wait(0.15):
            try:
                enabled = read_ptt_mic_enabled()
            except Exception:
                continue
            if enabled == last:
                continue
            last = enabled
            try:
                from livekit.agents.cli.cli import AgentsConsole

                AgentsConsole.get_instance().set_microphone_enabled(enabled)
                logger.info(
                    "push-to-talk: microphone input %s",
                    "resumed" if enabled else "paused",
                )
            except Exception:
                logger.warning("push-to-talk mic gate failed", exc_info=True)

    thread = threading.Thread(target=_watch, name="ptt-mic-watcher", daemon=True)
    thread.start()
    ctx.add_shutdown_callback(stop.set)


@server.rtc_session(agent_name="yaadhamma")
async def my_agent(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    browser = BrowserManager(headless=False)
    ctx.add_shutdown_callback(browser.close)

    assistant = Assistant(browser)

    # Realtime voice: Gemini Live does its own server-side turn detection and
    # interruption handling, so only preemptive generation is configured here.
    # A local voice detector tells us exactly when he stops talking, so the
    # reply-latency meter is accurate. Gemini still decides turns itself.
    session = AgentSession(
        vad=_local_vad(),
        turn_handling=TurnHandlingOptions(
            preemptive_generation={"enabled": True},
        ),
    )

    # Speed and cost: time every reply (you stop talking -> she starts
    # speaking) and add up the voice model's token use. One line per reply in
    # the console; a per-session summary in ~/.yaadhamma/voice_metrics.csv.
    metrics = VoiceMetrics(
        mode="realtime",
        # The detector reports "stopped" after this much silence; subtract it.
        speech_end_offset_s=config.LOCAL_VAD_SILENCE_S if config.LOCAL_VAD else 0.0,
        timing_reliable=config.LOCAL_VAD,
    )
    session.on("user_state_changed", metrics.on_user_state)
    session.on("agent_state_changed", metrics.on_agent_state)
    session.on("metrics_collected", metrics.on_metrics)
    ctx.add_shutdown_callback(metrics.write_summary)

    # Push-to-talk (v2 Stage 1): the daemon gates the mic through
    # ~/.yaadhamma/ptt-mic.json. Started for every session; harmless when the
    # daemon never writes the file (wake-word mode: always mic-on).
    _start_ptt_mic_watcher(ctx)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=assistant,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
        ),
    )

    # Join the room and connect to the user
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
