"""Local Aside CLI adapter, with an owned supervisor for every invocation.

The supervisor reads Aside's raw event stream, never its terminal transcript.
Closing its input also stops the owned Aside session when this process dies.
No provider SDK or additional Python dependency is required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import signal
import sys
import threading
import uuid

from contents_hub.runners.base import AgentRunError

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT = 3
MAX_PROMPT_BYTES = 512 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 600.0
CLEANUP_TIMEOUT_SECONDS = 15.0

_in_flight = 0
_lock = threading.Lock()


class AsideRunner:
    """Run subscription collection in the local, already signed-in browser.

    The CLI must support ``exec --log-dump``, ``session stop`` and
    ``session delete`` (verified with CLI 1.26.916.1741).
    Turn limits are enforced from completed assistant events: a tool-use turn
    at the limit fails immediately instead of permitting another turn.
    """

    browser_backend = "aside"

    def __init__(self, *, command: str = "aside") -> None:
        self._command = command

    async def run(
        self, prompt: str, *, max_turns: int = 30, timeout: float = 600.0,
    ) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise AgentRunError("Aside requires a non-empty collection prompt.")
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise AgentRunError("Aside prompt is too large. Reduce the collection scope.")
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            raise AgentRunError("Aside max_turns must be a positive integer.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise AgentRunError("Aside timeout must be positive and finite.")
        command = shutil.which(self._command)
        if command is None:
            raise AgentRunError(
                "Aside CLI was not found. Install it and add aside to PATH, "
                "then open Aside and sign in before retrying."
            )

        global _in_flight
        with _lock:
            if _in_flight >= MAX_IN_FLIGHT:
                raise AgentRunError(
                    "Aside collection capacity reached. Retry with --concurrency 3 or less."
                )
            _in_flight += 1
        run_id = uuid.uuid4().hex
        process = None
        error_code = None
        session_id = None
        try:
            # One supervisor owns the CLI, event file and browser session.
            # Its input remains open until finally, including on owner death.
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-m", "contents_hub.runners.aside_process",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                limit=MAX_RESULT_BYTES * 6 + 4096,
            ))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await spawn
                raise
            assert process.stdin is not None and process.stdout is not None
            request = {
                "command": command,
                "prompt": prompt,
                "max_turns": max_turns,
                "timeout": min(timeout, MAX_TIMEOUT_SECONDS),
            }
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readline(),
                    min(timeout, MAX_TIMEOUT_SECONDS) + CLEANUP_TIMEOUT_SECONDS,
                )
                result = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise AgentRunError(
                    "Aside supervisor returned an invalid result. "
                    f"Inspect collection sessions before retrying; reference {run_id}."
                ) from exc
            if not isinstance(result, dict):
                raise AgentRunError(f"Invalid Aside supervisor response; reference {run_id}.")
            error_code = result.get("error")
            session_id = result.get("session_id")
            if result.get("error") == "timeout":
                raise asyncio.TimeoutError("Aside collection timed out")
            if result.get("error"):
                recovery = (
                    f" Stop/delete owned Aside session {session_id} and retry."
                    if session_id else ""
                )
                raise AgentRunError(
                    f"Aside collection failed ({result['error']}). "
                    "Check that Aside is running and signed in, or reduce the collection scope. "
                    f"Reference: {run_id}.{recovery}"
                )
            text = result.get("text")
            if not isinstance(text, str) or not text.strip():
                raise AgentRunError(f"Aside returned no final answer; reference {run_id}.")
            if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
                raise AgentRunError("Aside result is too large. Reduce the collection scope.")
            logger.info(json.dumps({
                "event": "aside.run.completed", "run_id": run_id,
                "turns": result.get("turns"),
                "result_bytes": len(text.encode("utf-8")),
            }))
            return text
        except BaseException as exc:
            logger.warning(json.dumps({
                "event": "aside.run.failed", "run_id": run_id,
                "error_type": type(exc).__name__, "error_code": error_code,
                "session_id": session_id,
            }))
            raise
        finally:
            try:
                if process is not None:
                    assert process.stdin is not None
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), CLEANUP_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        # The supervisor normally handles EOF itself. Escalate
                        # only its dedicated group, never the user's browser.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
                        raise AgentRunError(
                            f"Aside cleanup timed out. Inspect owned sessions; reference {run_id}."
                        )
                    # Also end descendants if the supervisor itself crashed.
                    # The CLI's SIGTERM hook aborts its browser task.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    else:
                        await asyncio.sleep(0.1)
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        logger.warning(json.dumps({
                            "event": "aside.run.forced_group_cleanup", "run_id": run_id,
                        }))
            finally:
                with _lock:
                    _in_flight -= 1
