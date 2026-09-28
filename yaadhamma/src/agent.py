import inspect
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
from livekit.plugins import openai as lk_openai

import config
from actions import ActionRegistry
from audit import AuditLog
from browser import BrowserManager
from calendar_tools import CalendarTools
from digest import DigestTools
from echo_guard import EchoGuard, filter_echo_events
from failover_llm import FailoverLLM, maybe_wrap_with_failover
from file_tools import FileTools
from gmail_tools import GmailTools
from latency import VoiceMetrics
from mac_tools import MacTools
from memory_tools import MemoryTools
from meta_client import (
    MetaConfig,
    MetaConfigError,
    MetaRealtimeSTT,
    create_async_client,
)
from observation import ObservationTools
from orchestrator import Orchestrator, TaskTools, voice_tools
from outlook_tools import OutlookTools
from permissions import ApprovalManager, ApprovalTools
from prompts import AGENT_INSTRUCTIONS, VOICE_INSTRUCTIONS
from pronunciation import PronunciationTTS
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


def voice_components():
    """Build the (llm, stt, tts, mode) voice stack.

    Default ("realtime"): Gemini Live (YAADHAMMA_REALTIME_MODEL, normally
    gemini-3.8-live) hears, thinks and speaks in one model, with the voice
    Jeevan chose. On 3.8 Live, tools run in the background by default, so she
    can keep talking while a task works; results are spoken once she is idle.

    Rollback ("pipeline", set YAADHAMMA_VOICE_MODE=pipeline): the Meta path,
    Muse Spark thinks, Voice Transcribe listens, LiveKit Inference speaks,
    wrapped in the Gemini failover. Needs YAADHAMMA_MODEL_API_KEY.
    """
    cfg = MetaConfig.from_env()
    if cfg.voice_mode == "pipeline" and cfg.api_key:
        client = create_async_client(cfg)
        # Meta is OpenAI-compatible but not OpenAI: it rejects strict tool
        # schemas and non-"auto" tool_choice (400s). _strict_tool_schema=False
        # is the same escape hatch the plugin's third-party provider
        # constructors (SambaNova, Fireworks, ...) use.
        llm = lk_openai.LLM(
            model=cfg.voice_model, client=client, _strict_tool_schema=False
        )
        # Runtime failover: a hung Meta endpoint fails the turn over to a
        # Gemini text model (STT/TTS untouched). No-op without GOOGLE_API_KEY.
        llm = maybe_wrap_with_failover(llm)
        stt = MetaRealtimeSTT(cfg)
        # PronunciationTTS respells words the model mispronounces
        # ("Yaadhamma" -> "Yaah-dh-um-ah") just before synthesis.
        tts = PronunciationTTS(model=cfg.tts_model, voice=cfg.tts_voice)
        return llm, stt, tts, "pipeline"
    if cfg.voice_mode == "pipeline":
        logger.warning(
            "YAADHAMMA_VOICE_MODE=pipeline but YAADHAMMA_MODEL_API_KEY is not "
            "set; falling back to the Gemini Live realtime voice."
        )
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
        llm = google.beta.realtime.RealtimeModel(**options)
    except Exception as exc:
        raise MetaConfigError(
            "The Gemini Live voice could not start: check GOOGLE_API_KEY in "
            ".env.local (or set YAADHAMMA_VOICE_MODE=pipeline with a Meta key)."
        ) from exc
    return llm, None, None, "realtime"


def _local_vad():
    """The small on-device voice detector, or None if switched off."""
    if not config.LOCAL_VAD:
        return None
    from livekit.agents import inference

    return inference.VAD(min_silence_duration=config.LOCAL_VAD_SILENCE_S)


class Assistant(Agent):
    def __init__(self, browser: BrowserManager | None = None) -> None:
        # Echo guard first: it must exist before any STT node runs, so her
        # own TTS coming back through the mic is not heard as Jeevan.
        self.echo_guard = EchoGuard()
        # The voice stack first: pipeline (Muse Spark + Voice Transcribe +
        # LiveKit TTS) or the Gemini Live realtime fallback.
        self._voice_llm, self.voice_stt, self.voice_tts, self.voice_mode = (
            voice_components()
        )
        self.browser = browser or BrowserManager(headless=True)
        # One approval manager and one audit log shared by every toolset, so
        # an approval asked for in one path can be answered in any other, and
        # every consequential action is recorded in one place.
        self.audit_log = AuditLog()
        self.approvals = ApprovalManager(audit=self.audit_log)
        self.browser_tools = BrowserTools(self.browser, approvals=self.approvals)
        self.mac_tools = MacTools(approvals=self.approvals)
        self.file_tools = FileTools(approvals=self.approvals)
        self.gmail_tools = GmailTools(approvals=self.approvals)
        self.approval_tools = ApprovalTools(approvals=self.approvals)
        self.observation_tools = ObservationTools(self.browser)
        self.memory_tools = MemoryTools(audit=self.audit_log)
        self.outlook_tools = OutlookTools(approvals=self.approvals)
        self.whatsapp_tools = WhatsAppTools(
            browser=self.browser, approvals=self.approvals
        )
        self.calendar_tools = CalendarTools(approvals=self.approvals)
        toolsets = (
            self.browser_tools,
            self.mac_tools,
            self.file_tools,
            self.gmail_tools,
            self.approval_tools,
            self.observation_tools,
            self.memory_tools,
            self.outlook_tools,
            self.whatsapp_tools,
            self.calendar_tools,
        )
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
            tools = [
                *voice_tools(*toolsets),
                *TaskTools(self.orchestrator).tools,
                *DigestTools().tools,
            ]

        super().__init__(
            # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
            # See all available models at https://docs.livekit.io/agents/models/llm/
            llm=self._voice_llm,
            instructions=instructions + _current_time_note(),
            tools=[*tools, *self._end_call_tool.tools],
        )

    async def stt_node(self, audio, model_settings):
        """Default STT node plus the echo guard.

        Her own TTS can come back through the mic and be transcribed as if
        Jeevan said it (2026-09-24: it interrupted her own turns three
        times). Final transcripts that near-verbatim match what she just
        said are dropped before they can commit a user turn; everything
        else — including genuine barge-in — passes through untouched.
        """
        node = super().stt_node(audio, model_settings)
        if inspect.isawaitable(node):
            node = await node
        if node is None:
            return
        async for event in filter_echo_events(node, self.echo_guard):
            yield event


server = AgentServer()


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

    if assistant.voice_mode == "realtime":
        # Gemini Live does its own server-side turn detection and interruption
        # handling, so only preemptive generation is configured here.
        # A local voice detector tells us exactly when he stops talking, so the
        # reply-latency meter is accurate. Gemini still decides turns itself.
        session = AgentSession(
            vad=_local_vad(),
            turn_handling=TurnHandlingOptions(
                preemptive_generation={"enabled": True},
            ),
        )
    else:
        # Meta pipeline: Voice Transcribe listens, Muse Spark thinks,
        # LiveKit Inference speaks. The session handles turn detection.
        session = AgentSession(
            stt=assistant.voice_stt,
            tts=assistant.voice_tts,
        )
        # Failover is silent by Jeevan's explicit instruction: no spoken
        # "main brain is unreachable" message, ever. The FailoverLLM logs
        # every switch and switch-back structurally, and each backup-served
        # turn carries an invisible developer-context note (never spoken,
        # never in session history) so the assistant can answer truthfully
        # if Jeevan explicitly asks what happened. Nothing to wire here.
        voice_llm = assistant._voice_llm
        if isinstance(voice_llm, FailoverLLM):
            logger.info("LLM failover active and silent")

    # Speed and cost: time every reply (you stop talking -> she starts
    # speaking) and add up the voice model's token use. One line per reply in
    # the console; a per-session summary in ~/.yaadhamma/voice_metrics.csv.
    metrics = VoiceMetrics(
        mode=assistant.voice_mode,
        # The detector reports "stopped" after this much silence; subtract it.
        speech_end_offset_s=config.LOCAL_VAD_SILENCE_S
        if assistant.voice_mode == "realtime" and config.LOCAL_VAD
        else 0.0,
        timing_reliable=assistant.voice_mode != "realtime" or config.LOCAL_VAD,
    )
    session.on("user_state_changed", metrics.on_user_state)
    session.on("agent_state_changed", metrics.on_agent_state)
    session.on("metrics_collected", metrics.on_metrics)
    ctx.add_shutdown_callback(metrics.write_summary)

    # Echo guard: remember what she says so her own TTS coming back through
    # the mic is not transcribed as Jeevan (Assistant.stt_node drops it).
    @session.on("conversation_item_added")
    def _note_assistant_text(event) -> None:
        item = event.item
        if getattr(item, "role", None) == "assistant":
            text = getattr(item, "text_content", "") or ""
            if text.strip():
                assistant.echo_guard.note_assistant_text(text)

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

    if assistant.voice_mode != "realtime":
        # Pipeline mode only speaks after the user does; greet on connect so
        # Jeevan knows she's listening. This also exercises the TTS path
        # immediately instead of failing silently later.
        handle = session.generate_reply(
            # Meta rejects turns with no user/tool message, so the call
            # connecting doubles as the user message for this greeting.
            user_input="The call just connected.",
            instructions=(
                "Greet Jeevan briefly, suited to the time of day, "
                'for example "Evening, Sir." Nothing more. '
                # Meta's API only supports tool_choice="auto"; keep tools
                # quiet through instructions instead.
                "Do not call any tools for this greeting."
            ),
            allow_interruptions=True,
        )
        await handle
        if handle.exception() is not None:
            logger.error("Opening greeting failed: %r", handle.exception())


if __name__ == "__main__":
    cli.run_app(server)
