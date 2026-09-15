"""Requirement (2026-09-15): mam's actual working hours are two fixed
daily consultation blocks in US Central Time — 9:00-10:15 AM and
3:00-5:15 PM — not scattered slots spread evenly across the day. Two
things follow from that:

1. Every time shown to a human (the Teams brief's Date/Time lines, the
   leads sheet's date columns) should display in Central Time, not IST
   — an earlier version of this code assumed an India-based office, but
   Central Time availability points to a US-based attorney (clients are
   the India-based side of the relationship, not mam herself).
2. Instead of briefing each consultation ~15 minutes before its own
   start time (see app/brief_scheduler.py's original design), every
   consultation that falls inside one of the two blocks should be
   briefed by a single fixed deadline before that block begins — see
   compute_brief_deadline_utc below. A booking outside both blocks (in
   case Calendly availability ever allows one) falls back to the
   original ~15-minutes-before behavior instead of never being briefed.

Uses zoneinfo (stdlib) with the real "America/Chicago" tzdata entry,
not a fixed UTC offset — Central Time flips between CST and CDT with
US Daylight Saving Time, so a fixed offset would be wrong for roughly
half the year. requirements.txt pins the `tzdata` package so the IANA
database is available even on a minimal base image that doesn't ship
its own system tzdata.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

# (block start, block end, briefing deadline) — all in Central local time.
# The morning block's deadline is 60 minutes before it starts; the
# afternoon block's deadline is 30 minutes before it starts — both as
# explicitly specified by the requirement, not symmetrical by accident.
_BLOCKS: list[tuple[time, time, time]] = [
    (time(9, 0), time(10, 15), time(8, 0)),
    (time(15, 0), time(17, 15), time(14, 30)),
]


def _parse_utc(iso_str: str) -> Optional[datetime]:
    # Calendly's own API returns start_time with a trailing "Z" (e.g.
    # "2026-09-02T18:30:00Z") rather than "+00:00" — datetime.fromisoformat
    # only accepts "Z" directly on Python 3.11+, and this needs to behave
    # the same on whatever Python actually runs it (the Docker image is
    # 3.12, but that shouldn't be a silent requirement), so normalize it
    # by hand rather than relying on that version-specific leniency.
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_central(iso_str: Optional[str]) -> Optional[datetime]:
    """Parses a UTC ISO timestamp and returns the equivalent
    Central-Time-aware datetime, or None if it can't be parsed."""
    if not iso_str:
        return None
    dt = _parse_utc(iso_str)
    return dt.astimezone(CENTRAL) if dt else None


def compute_brief_deadline_utc(start_time_iso: Optional[str]) -> Optional[str]:
    """Returns the UTC ISO deadline by which this booking's Teams brief
    should be sent, based on which of mam's two consultation blocks its
    Central-Time start falls into. Returns None if it falls in neither
    block, so app/brief_scheduler.py falls back to briefing it ~15
    minutes before its own start time instead."""
    local = to_central(start_time_iso)
    if local is None:
        return None
    local_time = local.time()

    for block_start, block_end, deadline_time in _BLOCKS:
        if block_start <= local_time <= block_end:
            deadline_local = local.replace(
                hour=deadline_time.hour, minute=deadline_time.minute, second=0, microsecond=0
            )
            return deadline_local.astimezone(timezone.utc).isoformat()

    return None
