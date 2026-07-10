"""Shared UTC timestamp helpers.

The vault stores every timestamp as a timezone-aware UTC ISO-8601 string;
these helpers are the single spelling of that convention.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the vault's canonical format)."""
    return now_utc().isoformat()


__all__ = ["now_iso", "now_utc"]
