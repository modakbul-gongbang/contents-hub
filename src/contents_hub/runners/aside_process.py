"""Private one-shot supervisor; stdin EOF ends the owned browser work.

Aside's --log-dump is an external CLI event contract, verified against
1.26.916.1741. Unknown/missing events fail rather than treating terminal
progress, tool output, or prompt examples as a successful final answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

from contents_hub.runners.aside import MAX_PROMPT_BYTES, MAX_RESULT_BYTES

MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 10000
POLL_SECONDS = 0.05
CONTROL_TIMEOUT_SECONDS = 4.0
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SESSION = re.compile(r"^created new session: ([A-Za-z0-9_-]{1,128})$")


class CollectionError(Exception):
    session_id: str | None = None


async def _spawn(*args: str, **kwargs) -> asyncio.subprocess.Process:
    """The only CLI spawn point; recover a child even if spawn is cancelled."""
    task = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        child = await task
        if child.returncode is None:
            child.terminate()
        await child.wait()
        raise


async def _control(command: str, action: str, session_id: str) -> None:
    try:
        child = await _spawn(
            command, "session", action, session_id,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CollectionError(f"session_{action}_failed") from exc
    try:
        try:
            await asyncio.wait_for(child.wait(), CONTROL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise CollectionError(f"session_{action}_timeout") from exc
        if child.returncode:
            raise CollectionError(f"session_{action}_failed")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


class Events:
    def __init__(self, max_turns: int) -> None:
        self.max_turns = max_turns
        self.turns = 0
        self.count = 0
        self.text = ""
        self.finished = False

    def accept(self, event: dict) -> None:
        self.count += 1
        if self.count > MAX_EVENTS:
            raise CollectionError("event_limit")
        if not isinstance(event, dict):
            raise CollectionError("invalid_event")
        if event.get("type") == "agent_end":
            self.finished = True
        if event.get("type") != "message_end":
            return
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        self.turns += 1
        self.text = ""
        reason = message.get("stopReason")
        if message.get("errorMessage") or reason in {"error", "aborted"}:
            raise CollectionError("agent_error")
        if self.turns > self.max_turns or (
            self.turns == self.max_turns and reason == "toolUse"
        ):
            raise CollectionError("turn_limit")
        if reason in {"stop", "length"}:
            if reason == "length":
                raise CollectionError("incomplete_answer")
            content = message.get("content")
            if not isinstance(content, list):
                raise CollectionError("invalid_answer")
            self.text = "\n".join(
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ).strip()
            if len(self.text.encode("utf-8")) > MAX_RESULT_BYTES:
                raise CollectionError("result_limit")


async def _execute(request: dict) -> dict:
    command = request["command"]
    session_id = None
    child = None
    stderr_task = None
    completed = False

    async def read_stderr() -> None:
        nonlocal session_id
        assert child is not None and child.stderr is not None
        total = 0
        while line := await child.stderr.readline():
            total += len(line)
            if total > 64 * 1024:
                raise CollectionError("stderr_limit")
            match = _SESSION.fullmatch(_ANSI.sub("", line.decode("utf-8")).strip())
            if match:
                if session_id is not None:
                    raise CollectionError("invalid_session_header")
                session_id = match.group(1)

    with tempfile.TemporaryDirectory(prefix="contents-hub-aside-") as temp:
        events_path = Path(temp) / "events.jsonl"
        events_path.touch(mode=0o600)
        try:
            async with asyncio.timeout(request["timeout"]):
                child = await _spawn(
                    command, "exec", "--host", "local", "--permission", "guard",
                    "--log-dump", str(events_path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    limit=64 * 1024,
                )
                assert child.stdin is not None
                stderr_task = asyncio.create_task(read_stderr())
                child.stdin.write(request["prompt"].encode("utf-8"))
                await child.stdin.drain()
                child.stdin.close()
                events = Events(request["max_turns"])
                with events_path.open("rb") as stream:
                    pending = b""
                    while True:
                        if events_path.stat().st_size > MAX_EVENT_BYTES:
                            raise CollectionError("event_size_limit")
                        pending += stream.read(MAX_EVENT_BYTES + 1)
                        if len(pending) > MAX_EVENT_BYTES:
                            raise CollectionError("event_size_limit")
                        lines = pending.split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            events.accept(json.loads(line))
                        if stderr_task.done():
                            stderr_task.result()
                        if child.returncode is not None:
                            # Reap before the last read so flushed final events are
                            # consumed even if exit occurred during the poll.
                            await child.wait()
                            tail = stream.read(MAX_EVENT_BYTES + 1)
                            if tail:
                                pending += tail
                                continue
                            if pending or child.returncode != 0:
                                raise CollectionError("cli_failed")
                            break
                        await asyncio.sleep(POLL_SECONDS)
                await stderr_task
                if session_id is None or not events.finished or not events.text:
                    raise CollectionError("missing_final_answer")
                completed = True
                return {"text": events.text, "turns": events.turns}
        finally:
            # SIGTERM invokes Aside's own abort hook. Explicit session controls
            # also release the protocol resource, not just the OS process.
            if child is not None and child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), CONTROL_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    child.kill()
                    await child.wait()
            try:
                if stderr_task is not None:
                    await stderr_task
            finally:
                if session_id is not None:
                    try:
                        try:
                            if not completed:
                                await _control(command, "stop", session_id)
                        finally:
                            await _control(command, "delete", session_id)
                    except CollectionError as exc:
                        exc.session_id = session_id
                        raise


async def _main() -> None:
    reader = asyncio.StreamReader(limit=MAX_PROMPT_BYTES * 6 + 4096)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
    )
    raw = await reader.readline()
    request = json.loads(raw)
    owner = asyncio.create_task(reader.read(1))
    work = asyncio.create_task(_execute(request))
    try:
        done, _ = await asyncio.wait({owner, work}, return_when=asyncio.FIRST_COMPLETED)
        if owner in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return
        try:
            result = work.result()
        except asyncio.TimeoutError:
            result = {"error": "timeout"}
        except CollectionError as exc:
            result = {"error": str(exc), "session_id": exc.session_id}
        except Exception:
            result = {"error": "invalid_cli_events"}
        print(json.dumps(result), flush=True)
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(_main())
