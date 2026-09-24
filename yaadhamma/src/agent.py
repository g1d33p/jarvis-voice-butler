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
from browser import BrowserManager
from file_tools import FileTools
from mac_tools import MacTools
from observation import ObservationTools
from orchestrator import Orchestrator, TaskTools, voice_tools
from prompts import AGENT_INSTRUCTIONS, VOICE_INSTRUCTIONS
from task_manager import TaskStore
from tools import BrowserTools

load_dotenv(".env.local")  # also loaded by config; harmless twice


def _current_time_note() -> str:
    """Tell the model the local date and time, for greetings and scheduling."""
    now = datetime.now().astimezone()
    return (
        "\n\n# Current Time\n\n"
        f"When this conversation started it was {now:%A, %d %B %Y, %I:%M %p} "
        f"({now.tzname()}) on the user's Mac."
    )


class Assistant(Agent):
    def __init__(self, browser: BrowserManager | None = None) -> None:
        self.browser = browser or BrowserManager(headless=True)
        self.browser_tools = BrowserTools(self.browser)
        self.mac_tools = MacTools()
        self.file_tools = FileTools()
        self.observation_tools = ObservationTools(self.browser)
        toolsets = (
            self.browser_tools,
            self.mac_tools,
            self.file_tools,
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
            # llm=inference.LLM(model="google/gemma-4-31b-it"),
            llm=google.beta.realtime.RealtimeModel(
                model="gemini-3.1-flash-live-preview",
                voice="Enceladus",
                language="en-GB",
                tool_response_scheduling=genai_types.FunctionResponseScheduling.WHEN_IDLE,
            ),
            # To use a realtime model instead of a voice pipeline, replace the LLM
            # with a RealtimeModel and remove the STT/TTS from the AgentSession
            # (Note: This is for the OpenAI Realtime API. For other providers, see https://docs.livekit.io/agents/models/realtime/)
            # 1. Install livekit-agents[openai]
            # 2. Set OPENAI_API_KEY in .env.local
            # 3. Add `from livekit.plugins import openai` to the top of this file
            # 4. Replace the llm argument with:
            #     llm=openai.realtime.RealtimeModel(voice="marin")
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

    # Gemini realtime handles the voice input and output for this session.
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        # stt=inference.STT(model="deepgram/nova-3", language="en"),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        # tts=inference.TTS(
        #   model="fishaudio/s2.1-pro", voice="fa4c9eb3dccc4806b382b40d61c6b10a"
        # ),
        # Gemini Live does its own server-side turn detection and interruption
        # handling, so only preemptive generation is configured here.
        turn_handling=TurnHandlingOptions(
            preemptive_generation={"enabled": True},
        ),
        # Expressive mode injects the TTS provider's markup guide into the LLM prompt, so the model
        # emits inline delivery tags (emotion, pacing, non-verbal sounds) that the TTS renders and
        # the transcript never shows. Requires a TTS model that supports markup, such as the Fish
        # Audio model above.
        # expressive=True,
    )

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=Assistant(browser),
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
