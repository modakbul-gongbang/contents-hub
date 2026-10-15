"""Agent runner abstraction — decouples fetch/compile logic from a
specific LLM provider or agent harness (Claude SDK, Claude Code, Codex, etc.).

The base install defaults to a no-agent runner. Optional providers can be
selected with `CONTENTS_HUB_AGENT_RUNNER`.
"""

from __future__ import annotations

import os
from typing import Any

from contents_hub.runners.base import AgentRunner, AgentRunError
from contents_hub.runners.no_agent import NoAgentRunner

_DEFAULT: AgentRunner | None = None
_DEFAULT_TEXT: AgentRunner | None = None


def _selected_runner_name() -> str:
    return (os.environ.get("CONTENTS_HUB_AGENT_RUNNER") or "none").strip().lower()


def _build_runner(*, text_only: bool = False) -> AgentRunner:
    name = _selected_runner_name()
    if name in {"", "none", "off", "disabled"}:
        return NoAgentRunner()
    if name == "aside":
        # Aside is a browser collection backend. Keep text synthesis on the
        # existing deterministic fallback instead of opening browser sessions.
        if text_only:
            return NoAgentRunner()
        from contents_hub.runners.aside import AsideRunner

        return AsideRunner()
    if name == "claude-sdk":
        try:
            from contents_hub.runners.claude_sdk import ClaudeSDKRunner
        except ModuleNotFoundError as exc:
            raise AgentRunError(
                "CONTENTS_HUB_AGENT_RUNNER=claude-sdk requires the Claude optional "
                "extra to be installed."
            ) from exc
        return ClaudeSDKRunner(text_only=text_only)
    raise AgentRunError(
        f"Unknown CONTENTS_HUB_AGENT_RUNNER={name!r}. Supported values: none, claude-sdk, aside."
    )


def get_default_runner() -> AgentRunner:
    """Return the process-wide default runner (lazy-constructed)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = _build_runner()
    return _DEFAULT


def get_default_text_runner() -> AgentRunner:
    """Return the default runner for text-only synthesis calls.

    Injected non-browser runners remain usable by text synthesis. Aside uses
    the deterministic fallback; Claude synthesis omits browser plugins/tools
    in a fresh CLI process.
    """
    global _DEFAULT_TEXT
    if _DEFAULT is not None and get_browser_backend(_DEFAULT) != "aside":
        return _DEFAULT
    if _DEFAULT_TEXT is None:
        _DEFAULT_TEXT = _build_runner(text_only=True)
    return _DEFAULT_TEXT


def set_default_runner(runner: AgentRunner | None) -> None:
    """Override the default runner (primarily for tests)."""
    global _DEFAULT, _DEFAULT_TEXT
    _DEFAULT = runner
    _DEFAULT_TEXT = None


def get_browser_backend(runner: AgentRunner | None = None) -> str:
    """Resolve browser ownership for built-in and injected runners."""
    selected = runner if runner is not None else get_default_runner()
    return getattr(selected, "browser_backend", "chromux")


def __getattr__(name: str) -> Any:
    if name == "AsideRunner":
        from contents_hub.runners.aside import AsideRunner

        return AsideRunner
    if name == "ClaudeSDKRunner":
        from contents_hub.runners.claude_sdk import ClaudeSDKRunner

        return ClaudeSDKRunner
    raise AttributeError(name)


__all__ = [
    "AgentRunner",
    "AgentRunError",
    "AsideRunner",
    "ClaudeSDKRunner",
    "NoAgentRunner",
    "get_default_runner",
    "get_browser_backend",
    "get_default_text_runner",
    "set_default_runner",
]
