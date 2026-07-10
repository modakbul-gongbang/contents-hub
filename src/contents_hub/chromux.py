"""Shared Chromux profile policy for contents-hub.

The contents-hub Chrome profile has a single owner at a time:

- headed Chrome is used for both human foreground sign-in / inspection and
  agent-driven fetches;
- headless Chrome is still accepted for existing/background automation;
- background fetches may reuse an already-headed profile instead of failing or
  trying to mode-switch Chrome under the same user-data-dir. Agent fetches use
  chromux background tab creation so new tabs do not steal focus.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Iterable
from contextlib import contextmanager
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from contents_hub.naming import CHROMUX_PROFILE

logger = logging.getLogger(__name__)

CHROMUX_PROFILE_NAME = CHROMUX_PROFILE.canonical
PROFILE_IN_FOREGROUND_REASON = "profile_in_foreground"
PROFILE_SWITCH_NEEDS_CONFIRM_REASON = "profile_switch_needs_confirmation"
AUTH_PROFILE_UNAVAILABLE_REASON = "auth_profile_unavailable"

_ACTIVE_FETCH_SESSIONS: contextvars.ContextVar[set[str] | None] = (
    contextvars.ContextVar("contents_hub_chromux_sessions", default=None)
)
_ALLOW_FOREGROUND_FETCH: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "contents_hub_allow_foreground_fetch",
    default=False,
)
_PROFILE_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "contents_hub_chromux_profile_override",
    default=None,
)


@dataclass(frozen=True)
class ChromuxExplorationSessionError(RuntimeError):
    """Structured profile/auth blocker for foreground exploration runs."""

    message: str
    status: str
    profile: str
    previous_state: str
    failure_reason: str

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def to_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "status": self.status,
            "profile": self.profile,
            "previous_state": self.previous_state,
            "failure_reason": self.failure_reason,
            "error": self.message,
        }


def _chromux_bin() -> str:
    return shutil.which("chromux") or "chromux"


def _chromux_profile_root() -> Path:
    """Return the chromux profile root used for compatibility detection."""
    return Path(os.environ.get("CHROMUX_PROFILES_DIR") or Path.home() / ".chromux" / "profiles")


def _chromux_profile_exists(profile: str) -> bool:
    return (_chromux_profile_root() / profile).exists()


def _running_chromux_profiles() -> set[str]:
    try:
        out = subprocess.run(
            [_chromux_bin(), "ps"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return set()

    profiles: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            profiles.add(parts[0])
    return profiles


def resolve_chromux_profile(profile: str | None = None) -> str:
    """Return the profile to use for a new Chromux operation."""
    if profile:
        return profile
    if override := _PROFILE_OVERRIDE.get():
        return override

    running = _running_chromux_profiles()
    if CHROMUX_PROFILE_NAME in running:
        return CHROMUX_PROFILE_NAME
    if _chromux_profile_exists(CHROMUX_PROFILE_NAME):
        return CHROMUX_PROFILE_NAME
    return CHROMUX_PROFILE_NAME


@contextmanager
def chromux_profile_override(profile: str | None):
    """Pin Chromux calls in the current context to a specific profile."""
    if not profile:
        yield
        return
    token = _PROFILE_OVERRIDE.set(profile)
    try:
        yield
    finally:
        _PROFILE_OVERRIDE.reset(token)


def _chromux_env(profile: str | None = None) -> dict[str, str]:
    profile = resolve_chromux_profile(profile)
    return {**os.environ, "CHROMUX_PROFILE": profile}


def chromux_automation_env(profile: str | None = None) -> dict[str, str]:
    """Return env for agent/background Chromux automation.

    Headed mode keeps one normal Chrome profile alive for automation without
    bouncing between headless and headed modes. Background tab creation keeps
    ``chromux open`` from activating Chrome on every new session.
    """
    return {
        **_chromux_env(profile),
        "CHROMUX_LAUNCH_MODE": "headed",
        "CHROMUX_OPEN_BACKGROUND": "1",
    }


def _chromux_profile_runtime(profile: str) -> tuple[int | None, int | None]:
    try:
        out = subprocess.run(
            [_chromux_bin(), "ps"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return None, None

    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == profile:
            try:
                port = int(parts[1])
            except ValueError:
                port = None
            try:
                pid = int(parts[2]) if len(parts) >= 3 else None
            except ValueError:
                pid = None
            return port, pid
    return None, None


def chromux_profile_state(profile: str | None = None) -> str:
    """Return ``not_running``, ``headless``, or ``headed``."""
    profile = resolve_chromux_profile(profile)
    port, _pid = _chromux_profile_runtime(profile)
    if port is None:
        return "not_running"

    try:
        resp = httpx.get(f"http://localhost:{port}/json/version", timeout=2)
        user_agent = resp.json().get("User-Agent", "")
    except Exception:
        return "not_running"
    if "HeadlessChrome" in user_agent:
        return "headless"
    return "headed"


def is_chromux_profile_in_foreground(
    profile: str | None = None,
) -> bool:
    return chromux_profile_state(profile) == "headed"


def is_foreground_fetch_allowed() -> bool:
    return _ALLOW_FOREGROUND_FETCH.get()


def open_chromux_headed(
    url: str | None,
    *,
    session: str | None = None,
    confirmed: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """Open ``url`` or just launch ``profile`` in headed mode.

    If the profile is already running background automation, the first call returns
    ``status=needs_confirm`` instead of silently killing an in-flight fetch.
    """
    profile = resolve_chromux_profile(profile)
    chromux_bin = _chromux_bin()
    env = _chromux_env(profile)
    state = chromux_profile_state(profile)

    if state == "headless" and not confirmed:
        return {
            "status": "needs_confirm",
            "url": url,
            "previous_state": state,
            "error": (
                f"Background browser work is running in {state} mode for "
                f"chromux profile '{profile}'. To open a visible browser, "
                "contents-hub must stop that background browser and reopen it "
                "headed. Any active fetch on this profile may fail. Re-submit "
                "with confirmed=true to continue."
            ),
        }

    try:
        if state == "headless":
            subprocess.run(
                [chromux_bin, "kill", profile],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        if state != "headed":
            subprocess.Popen(
                [chromux_bin, "launch", profile],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        open_url = url
        if open_url is None and state == "headed":
            open_url = "about:blank"
        if open_url is not None:
            subprocess.Popen(
                [chromux_bin, "open", session or "view", open_url],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as exc:  # pragma: no cover - subprocess spawn failures
        return {
            "status": "error",
            "url": url,
            "previous_state": state,
            "error": f"chromux launch failed: {exc}",
        }

    return {
        "status": "opened" if url is not None else "launched",
        "url": url,
        "previous_state": state,
        "error": None,
    }


def kill_chromux_profile(profile: str | None = None) -> dict[str, Any]:
    """Kill the profile so future background fetches can auto-spawn headed."""
    profile = resolve_chromux_profile(profile)
    chromux_bin = _chromux_bin()
    env = _chromux_env(profile)
    state = chromux_profile_state(profile)
    if state == "not_running":
        return {"status": "not_running", "previous_state": state, "error": None}

    try:
        subprocess.run(
            [chromux_bin, "kill", profile],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - subprocess failures
        return {
            "status": "error",
            "previous_state": state,
            "error": f"chromux kill failed: {exc}",
        }
    return {"status": "killed", "previous_state": state, "error": None}


def prepare_chromux_for_background_fetch(profile: str | None = None) -> dict[str, Any]:
    """Ensure the shared profile can be used by an agent-driven fetch.

    Chromux launch modes only apply when the profile starts. If the profile is
    already visible/headed, a fetch can reuse that live Chrome instead of failing
    or attempting a headed -> headless mode switch.
    """
    profile = resolve_chromux_profile(profile)
    state = chromux_profile_state(profile)
    if state == "headed" and not is_foreground_fetch_allowed():
        logger.info(
            "agent chromux fetch reusing visible headed profile: profile=%s",
            profile,
        )
        return {
            "ok": True,
            "status": "foreground_reused",
            "profile": profile,
            "error": None,
        }
    return {"ok": True, "status": "ready", "profile": profile, "error": None}


def track_chromux_session(session_id: str) -> None:
    sessions = _ACTIVE_FETCH_SESSIONS.get()
    if sessions is not None:
        sessions.add(session_id)


def close_chromux_session(
    session_id: str,
    *,
    profile: str | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    profile = resolve_chromux_profile(profile)
    return subprocess.run(
        [_chromux_bin(), "close", session_id],
        env=_chromux_env(profile),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def list_chromux_sessions(profile: str | None = None) -> set[str]:
    """Return active chromux tab/session ids for ``profile``.

    This is intentionally best-effort. Cleanup still closes explicitly tracked
    sessions when listing fails, but the list diff lets us catch tabs opened via
    Bash or other direct chromux calls that bypass ``track_chromux_session``.
    """
    profile = resolve_chromux_profile(profile)
    try:
        proc = subprocess.run(
            [_chromux_bin(), "list"],
            env=_chromux_env(profile),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 - listing must not break cleanup
        logger.debug("chromux list failed for profile=%s: %s", profile, exc)
        return set()
    if proc.returncode != 0:
        logger.debug(
            "chromux list returned %s for profile=%s: %s",
            proc.returncode,
            profile,
            (proc.stderr or proc.stdout or "").strip(),
        )
        return set()
    try:
        parsed = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return set()
    if isinstance(parsed, dict):
        return {str(key) for key in parsed.keys() if str(key).strip()}
    if isinstance(parsed, list):
        ids: set[str] = set()
        for item in parsed:
            if isinstance(item, str) and item.strip():
                ids.add(item)
            elif isinstance(item, dict):
                session_id = (
                    item.get("session")
                    or item.get("session_id")
                    or item.get("id")
                )
                if session_id:
                    ids.add(str(session_id))
        return ids
    return set()


@asynccontextmanager
async def chromux_fetch_session_cleanup(
    fallback_sessions: Iterable[str] = (),
    *,
    profile: str | None = None,
    close_new_sessions: bool = True,
):
    """Track chromux sessions opened during one fetch and close them after."""
    resolved_profile = resolve_chromux_profile(profile)
    before_sessions = (
        list_chromux_sessions(resolved_profile) if close_new_sessions else set()
    )
    sessions: set[str] = set()
    token = _ACTIVE_FETCH_SESSIONS.set(sessions)
    try:
        yield sessions
    finally:
        _ACTIVE_FETCH_SESSIONS.reset(token)
        if close_new_sessions:
            after_sessions = list_chromux_sessions(resolved_profile)
            sessions.update(after_sessions - before_sessions)
        for session_id in sorted(sessions | set(fallback_sessions)):
            try:
                proc = close_chromux_session(session_id, profile=resolved_profile)
            except Exception as exc:  # noqa: BLE001
                logger.warning("chromux close failed for %s: %s", session_id, exc)
                continue
            if proc.returncode != 0:
                logger.debug(
                    "chromux close returned %s for %s: %s",
                    proc.returncode,
                    session_id,
                    (proc.stderr or proc.stdout or "").strip(),
                )


@asynccontextmanager
async def chromux_foreground_fetch():
    """Allow agent Chromux tools to drive the visible profile for one fetch."""
    token = _ALLOW_FOREGROUND_FETCH.set(True)
    try:
        yield
    finally:
        _ALLOW_FOREGROUND_FETCH.reset(token)


@asynccontextmanager
async def chromux_exploration_session(
    url: str | None = None,
    *,
    session: str | None = None,
    confirmed: bool = False,
    profile: str | None = None,
    fallback_sessions: Iterable[str] = (),
):
    """Run exploration validation/manual collection in foreground/headed Chromux.

    The context enforces the exploration browser ownership contract:
    foreground start does not silently interrupt a headless owner, background
    guards are bypassed only inside this explicit foreground context, and every
    tracked run-scoped session is closed on success, failure, or timeout.
    """
    resolved_profile = resolve_chromux_profile(profile)
    launch = open_chromux_headed(
        url,
        session=session,
        confirmed=confirmed,
        profile=resolved_profile,
    )
    status = str(launch.get("status") or "")
    if status == "needs_confirm":
        raise ChromuxExplorationSessionError(
            str(launch.get("error") or "chromux foreground start needs confirmation"),
            status=status,
            profile=resolved_profile,
            previous_state=str(launch.get("previous_state") or ""),
            failure_reason=PROFILE_SWITCH_NEEDS_CONFIRM_REASON,
        )
    if status == "error":
        raise ChromuxExplorationSessionError(
            str(launch.get("error") or "chromux foreground start failed"),
            status=status,
            profile=resolved_profile,
            previous_state=str(launch.get("previous_state") or ""),
            failure_reason=AUTH_PROFILE_UNAVAILABLE_REASON,
        )

    with chromux_profile_override(resolved_profile):
        async with chromux_fetch_session_cleanup(
            fallback_sessions,
            profile=resolved_profile,
        ) as sessions:
            if session and url is not None:
                track_chromux_session(session)
            async with chromux_foreground_fetch():
                yield {
                    "profile": resolved_profile,
                    "launch": launch,
                    "session_ids": sessions,
                }


__all__ = [
    "CHROMUX_PROFILE_NAME",
    "PROFILE_IN_FOREGROUND_REASON",
    "PROFILE_SWITCH_NEEDS_CONFIRM_REASON",
    "AUTH_PROFILE_UNAVAILABLE_REASON",
    "ChromuxExplorationSessionError",
    "chromux_exploration_session",
    "chromux_fetch_session_cleanup",
    "chromux_foreground_fetch",
    "chromux_automation_env",
    "chromux_profile_override",
    "chromux_profile_state",
    "close_chromux_session",
    "list_chromux_sessions",
    "is_foreground_fetch_allowed",
    "is_chromux_profile_in_foreground",
    "kill_chromux_profile",
    "open_chromux_headed",
    "prepare_chromux_for_background_fetch",
    "resolve_chromux_profile",
    "track_chromux_session",
]
