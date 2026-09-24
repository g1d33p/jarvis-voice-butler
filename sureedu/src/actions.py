"""The action registry: every tool Sureedu has, callable by the background brain.

The voice agent and the task orchestrator share the same tool objects (and so
the same browser, approvals and safety checks). The registry just gives the
orchestrator a uniform way to describe the tools to its model and call them.
"""

from __future__ import annotations

import json
from typing import Any

from google.genai import types
from livekit.agents import llm
from livekit.agents.llm import ToolError

# Tool results longer than this are cut before going back to the model.
MAX_RESULT_CHARS = 6_000


class ActionRegistry:
    def __init__(self, *toolsets: object, exclude: set[str] | None = None) -> None:
        exclude = exclude or set()
        self._tools: dict[str, Any] = {}
        for toolset in toolsets:
            for tool in toolset.tools:  # type: ignore[attr-defined]
                if tool.id not in exclude:
                    self._tools[tool.id] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def declarations(self) -> list[types.FunctionDeclaration]:
        """Describe the tools in the Gemini function-calling format."""
        schemas = llm.ToolContext(list(self._tools.values())).parse_function_tools(
            "google", use_parameters_json_schema=True
        )
        return [types.FunctionDeclaration.model_validate(schema) for schema in schemas]

    async def call(
        self, name: str, args: dict[str, Any] | None, context: object
    ) -> dict[str, Any]:
        """Run one tool and return {"ok": ..., "result" or "error": ...}.

        Tool failures are returned as data, not raised, so the orchestrator's
        model can read them and decide what to do next.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"There is no tool named {name!r}."}
        try:
            result = await tool(context, **(args or {}))
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except TypeError as exc:
            return {"ok": False, "error": f"Wrong arguments for {name}: {exc}"}
        except Exception as exc:  # a tool bug must not crash the whole task
            return {"ok": False, "error": f"{name} failed unexpectedly: {exc}"}
        return {"ok": True, "result": _trim(result)}


def _trim(result: Any) -> Any:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return result
    return text[:MAX_RESULT_CHARS] + " ...(cut)"
