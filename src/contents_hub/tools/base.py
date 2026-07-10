"""ToolSpec — frozen dataclass describing one agent-callable tool.

A `ToolSpec` is the runner-agnostic record for a single tool exposed to the
agent executor. The `ClaudeSDKRunner` SDK adapter (see `runners/claude_sdk.py`)
converts each `ToolSpec.input_schema` to the Claude Agent SDK's native tool
format before passing it into `claude_agent_sdk.query()`.

Field shape (frozen by contracts.md → "ToolSpec"):

    name:         str               — identifier referenced by recipe body text
    description:  str               — human-readable purpose
    input_schema: dict              — Anthropic-native JSON Schema dict
    handler:      Callable[..., Awaitable[str]]
                                     — async coroutine returning a string

`input_schema` intentionally uses a plain dict (Anthropic-native JSON Schema)
to avoid pulling in Pydantic and to keep specs JSON-serializable for
debugging / future cross-runner adapters.

Handlers are coroutine functions; they may take arbitrary keyword arguments
that the runner unpacks from the agent's tool-call arguments and must return
a string (per the existing `AgentRunner.run` text-only contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

# Type alias for tool handler coroutine functions.
# Runners pass the agent's tool-call arguments as keyword arguments; the
# handler returns the tool result as a string (JSON-encoded payloads are
# allowed and common — the agent reads the text and decides what to do).
ToolHandler = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    """One agent-callable tool definition.

    Frozen so registries can hand specs out by reference without callers
    being able to mutate them in place.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def __post_init__(self) -> None:  # pragma: no cover - simple validation
        if not self.name:
            raise ValueError("ToolSpec.name must be a non-empty string")
        if not isinstance(self.input_schema, dict):
            raise TypeError(
                f"ToolSpec.input_schema must be a dict, got {type(self.input_schema).__name__}"
            )
        if not callable(self.handler):
            raise TypeError("ToolSpec.handler must be callable")


__all__ = ["ToolSpec", "ToolHandler"]
