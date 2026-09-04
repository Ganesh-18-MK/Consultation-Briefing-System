"""Client for Fireflies.ai — the meeting bot that actually captures each
consultation (Requirement 2). Fireflies auto-joins Teams meetings from
the manager's connected calendar (set up in the Fireflies dashboard, not
here — see README) and is the source of transcript data for both the
post-meeting notes and the mid-call live catch-up. Replaces the earlier
Microsoft Graph transcript-polling approach: Fireflies documents an
explicit live-caption API (`active_meetings` + `is_live`), which is more
reliable than Graph's undocumented mid-meeting transcript behavior.

Microsoft Graph is still used elsewhere (app/graph_client.py +
app/teams_delivery.py) but only for *delivering* messages to the
manager's Teams chat — Fireflies is the only transcript source now.

API docs: https://docs.fireflies.ai/graphql-api
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

GRAPHQL_URL = "https://api.fireflies.ai/graphql"


class FirefliesError(Exception):
    pass


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.fireflies_api_key}",
        "Content-Type": "application/json",
    }


def _query(query: str, variables: dict) -> dict:
    resp = requests.post(
        GRAPHQL_URL,
        headers=_headers(),
        json={"query": query, "variables": variables},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise FirefliesError(str(body["errors"]))
    return body.get("data") or {}


def normalize_join_url(url: Optional[str]) -> str:
    """Fireflies' meeting_link and Calendly's join_url should point at
    the same Teams meeting, but query-string params/trailing slashes can
    differ between where each side captured the URL. Normalize both
    sides before comparing rather than requiring an exact string match."""
    if not url:
        return ""
    parts = urlsplit(url.strip().lower())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


# ─── Webhook signature verification ────────────────────────────────────

def verify_webhook_signature(raw_body: bytes, signature_header: Optional[str], secret: str) -> bool:
    """Fireflies signs webhook deliveries with `x-hub-signature`: a
    SHA-256 HMAC of the raw request body using the secret configured in
    Fireflies' Developer Settings."""
    if not secret:
        log.warning("FIREFLIES_WEBHOOK_SECRET not set — skipping signature check")
        return True
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # Fireflies' header may or may not include a "sha256=" prefix depending
    # on version; strip it if present before comparing.
    provided = signature_header.split("=", 1)[-1] if "=" in signature_header else signature_header
    return hmac.compare_digest(expected, provided)


# ─── Transcript / meeting queries ──────────────────────────────────────

_TRANSCRIPT_QUERY = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    meeting_link
    organizer_email
    date
    participants
    sentences {
      speaker_name
      text
    }
  }
}
"""


def get_transcript(meeting_id: str) -> Optional[dict]:
    """Full (or, if the meeting is still live, live-caption) transcript
    for a Fireflies meeting id. Returns None if Fireflies has no record
    of it (e.g. called too early, or the id is wrong)."""
    data = _query(_TRANSCRIPT_QUERY, {"id": meeting_id})
    return data.get("transcript")


def transcript_text(transcript: dict) -> str:
    """Fireflies' `sentences` list -> plain "Speaker: line" text, same
    shape app/summarizer.py already expects."""
    lines = []
    for sentence in transcript.get("sentences") or []:
        speaker = sentence.get("speaker_name") or "Speaker"
        text = (sentence.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


_ACTIVE_MEETINGS_QUERY = """
query ActiveMeetings($email: String, $states: [MeetingState!]) {
  active_meetings(input: { email: $email, states: $states }) {
    id
    title
    organizer_email
    meeting_link
    start_time
    end_time
    state
  }
}
"""


def find_active_meeting_id(organizer_email: str, join_url: str) -> Optional[str]:
    """Looks for a currently-in-progress Fireflies meeting for this
    organizer whose link matches the booking's join_url. Used by the
    live catch-up job — see its module docstring for the caveat that
    Fireflies' live-caption support should be confirmed against a real
    in-progress meeting."""
    target = normalize_join_url(join_url)
    if not target:
        return None
    data = _query(_ACTIVE_MEETINGS_QUERY, {"email": organizer_email, "states": ["active"]})
    for meeting in data.get("active_meetings") or []:
        if normalize_join_url(meeting.get("meeting_link")) == target:
            return meeting.get("id")
    return None


def get_live_transcript_text(organizer_email: str, join_url: str) -> Optional[str]:
    """Convenience wrapper for live_catchup.py: join_url -> whatever
    live-caption text Fireflies currently has for that meeting, or None
    if no matching in-progress meeting is found."""
    meeting_id = find_active_meeting_id(organizer_email, join_url)
    if not meeting_id:
        return None
    transcript = get_transcript(meeting_id)
    if not transcript:
        return None
    return transcript_text(transcript)
