"""No-agent runner used by the runtime-neutral base install."""

from __future__ import annotations

from contents_hub.runners.base import AgentRunError


class NoAgentRunner:
    """AgentRunner implementation that fails with an actionable message."""

    async def run(
        self,
        prompt: str,
        *,
        max_turns: int = 30,
        timeout: float = 600.0,
    ) -> str:
        raise AgentRunError(
            "Agent-backed collection or synthesis requires an optional runner. "
            "For browser collection, install Aside and set CONTENTS_HUB_AGENT_RUNNER=aside. "
            "For synthesis or exploration, install the Claude extra and set "
            "CONTENTS_HUB_AGENT_RUNNER=claude-sdk, "
            "or use deterministic HTTP/RSS/raw/dashboard flows."
        )
