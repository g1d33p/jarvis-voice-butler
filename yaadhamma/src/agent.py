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
from file_tools import FileTools
from mac_tools import MacTools
from meta_client import (
    MetaConfig,
    MetaConfigError,
    MetaRealtimeSTT,
    create_async_client,
)
from observation import ObservationTools
from orchestrator import Orchestrator, TaskTools, voice_tools
from permissions import ApprovalManager, ApprovalTools
from prompts import AGENT_INSTRUCTIONS, VOICE_INSTRUCTIONS
from pronunciation import PronunciationTTS
from task_manager import TaskStore
from tools import BrowserTools

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

    Default ("pipeline"): Muse Spark thinks, Voice Transcribe listens, and
    LiveKit Inference speaks. Falls back to the old Gemini Live realtime path
    with a loud warning when no Meta key is configured; YAADHAMMA_VOICE_MODE
    set to "realtime" forces that path.
    """
    cfg = MetaConfig.from_env()
    if cfg.voice_mode != "realtime" and cfg.api_key:
        client = create_async_client(cfg)
        # Meta is OpenAI-compatible but not OpenAI: it rejects strict tool
        # schemas and non-"auto" tool_choice (400s). _strict_tool_schema=False
        # is the same escape hatch the plugin's third-party provider
        # constructors (SambaNova, Fireworks, ...) use.
        llm = lk_openai.LLM(
            model=cfg.voice_model, client=client, _strict_tool_schema=False
        )
        stt = MetaRealtimeSTT(cfg)
        # PronunciationTTS respells words the model mispronounces
        # ("Yaadhamma" -> "Yaah-dh-um-ah") just before synthesis.
        tts = PronunciationTTS(model=cfg.tts_model, voice=cfg.tts_voice)
        return llm, stt, tts, "pipeline"
    if cfg.voice_mode != "realtime":
        logger.warning(
            "YAADHAMMA_MODEL_API_KEY is not set; falling back to the Gemini "
            "Live realtime voice path. Set the key in .env.local for the "
            "Meta pipeline."
        )
    try:
        llm = google.beta.realtime.RealtimeModel(
            model="gemini-3.1-flash-live-preview",
            voice="Enceladus",
            language="en-GB",
            tool_response_scheduling=genai_types.FunctionResponseScheduling.WHEN_IDLE,
        )
    except Exception as exc:
        raise MetaConfigError(
            "No voice backend is usable: set YAADHAMMA_MODEL_API_KEY for the "
            "Meta pipeline or GOOGLE_API_KEY for the Gemini realtime fallback."
        ) from exc
    return llm, None, None, "realtime"


class Assistant(Agent):
    def __init__(self, browser: BrowserManager | None = None) -> None:
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
        self.approval_tools = ApprovalTools(approvals=self.approvals)
        self.observation_tools = ObservationTools(self.browser)
        toolsets = (
            self.browser_tools,
            self.mac_tools,
            self.file_tools,
            self.approval_tools,
            self.observation_tools,
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
            tools = [*voice_tools(*toolsets), *TaskTools(self.orchestrator).tools]

        super().__init__(
            # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
            # See all available models at https://docs.livekit.io/agents/models/llm/
            llm=self._voice_llm,
            instructions=instructions + _current_time_note(),
            tools=[*tools, *self._end_call_tool.tools],
        )


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
        session = AgentSession(
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
