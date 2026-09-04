"""Thin client for the parts of the Calendly API the webhook payload
doesn't carry directly: the scheduled event's start_time, the assigned
host (attorney), and the conferencing join_url. See Section 6 of the
plan — the exact payload shape should be checked against a real webhook
delivery before go-live; this module is the one place that assumption
lives, so it's cheap to fix later.
"""
from __future__ import annotations

from typing import Optional, TypedDict

import requests

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

API_BASE = "https://api.calendly.com"


class EventDetails(TypedDict):
    start_time: Optional[str]
    attorney_email: Optional[str]
    join_url: Optional[str]


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.calendly_api_token}"}


def get_event_details(event_uri: str) -> EventDetails:
    """Fetch start_time / host email / Teams join_url for a scheduled
    event. Returns Nones for anything unavailable (missing token,
    request failure, or a location type that isn't a Teams conference)
    rather than raising — a booking is still worth recording even if we
    can't enrich it yet."""
    details: EventDetails = {"start_time": None, "attorney_email": None, "join_url": None}

    if not settings.calendly_api_token:
        log.warning("CALENDLY_API_TOKEN not set — cannot fetch event details for %s", event_uri)
        return details

    try:
        resp = requests.get(event_uri, headers=_headers(), timeout=10)
        resp.raise_for_status()
        event = resp.json().get("resource", {})
    except requests.RequestException as exc:
        log.error("Failed to fetch Calendly event %s: %s", event_uri, exc)
        return details

    details["start_time"] = event.get("start_time")

    location = event.get("location") or {}
    if location.get("type") in ("microsoft_teams_conference", "ms_teams_conference"):
        details["join_url"] = location.get("join_url") or location.get("data", {}).get("join_url")

    # Host / attorney: first entry in event_memberships. For multi-host
    # events this takes the first one; adjust if a firm ever runs
    # consultations with co-hosts.
    memberships = event.get("event_memberships") or []
    if memberships:
        details["attorney_email"] = memberships[0].get("user_email")

    return details


def extract_uuid(resource_uri: str) -> str:
    """Calendly resource URIs end in .../<uuid>. Used as our stable
    booking identifier."""
    return resource_uri.rstrip("/").rsplit("/", 1)[-1]
