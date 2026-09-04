"""Requirement 3: while a consultation is in progress, periodically
check whether Fireflies has new live-caption content and, if so, push a
short "what's been discussed so far" recap into the manager's private
Teams chat — the same chat the pre-call brief went to. Framed as "catch
someone up if they joined late," but it lands with the manager either
way (see README for why: the alternative, posting into the Teams
meeting's own chat, is visible to the client too).

Run every ~5 minutes via cron (see deploy/crontab.txt).

Data source: Fireflies' `active_meetings` + live-caption `sentences`
(see app/fireflies_client.py) — documented as returning real-time
captions for an in-progress meeting. Still worth confirming against a
real live consultation early; if it turns out Fireflies' live captions
lag or don't populate reliably, this job just finds nothing and skips
(same graceful-degradation pattern as everywhere else in this pipeline).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, fireflies_client, summarizer, teams_delivery
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _client_name(client_id: str) -> str:
    client = db.get_client(client_id)
    return (client and (client.get("name") or client.get("email"))) or client_id


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def process_booking(booking) -> None:
    if not booking["attorney_email"]:
        log.warning("Booking %s has no manager email — cannot look up its Fireflies meeting", booking["id"])
        return

    manager_email = booking["attorney_email"]
    manager_upn = settings.attorney_upn(manager_email)

    try:
        transcript_text = fireflies_client.get_live_transcript_text(manager_email, booking["join_url"])
    except Exception:
        log.exception("Fireflies error fetching live captions for booking %s", booking["id"])
        return

    if not transcript_text or not transcript_text.strip():
        log.info("No live caption content yet for in-progress booking %s (will check again)", booking["id"])
        return

    transcript_hash = _hash(transcript_text)
    if transcript_hash == booking["catchup_last_transcript_hash"]:
        log.info("No new caption content for booking %s since last push — skipping", booking["id"])
        return

    if len(transcript_text.strip()) < settings.catchup_min_new_chars:
        log.info(
            "Live captions for booking %s are too short (%d chars) to summarize yet — skipping",
            booking["id"],
            len(transcript_text.strip()),
        )
        return

    client_name = _client_name(booking["client_id"])
    summary = summarizer.summarize_live_catchup(client_name, transcript_text)

    posted = teams_delivery.send_manager_message(
        manager_upn,
        title=f"Live catch-up: {client_name} (in progress)",
        lines=[summary],
    )

    # Record the push (and the transcript hash, for de-dup) regardless of
    # delivery success — same reasoning as brief_scheduler: a failed
    # Teams call shouldn't cause the same content to be summarized and
    # retried every 5 minutes for the life of the meeting.
    db.record_catchup_push(booking["id"], transcript_hash)

    if posted:
        log.info("Pushed live catch-up for booking %s (%s)", booking["id"], client_name)
    else:
        log.error("Recorded catch-up push for booking %s but the Teams message failed", booking["id"])


def run() -> None:
    if not settings.catchup_enabled:
        log.info("Live catch-up disabled (CATCHUP_ENABLED=false) — skipping")
        return

    candidates = db.get_in_progress_bookings(
        max_duration_minutes=settings.catchup_max_meeting_minutes,
        max_pushes=settings.catchup_max_pushes,
    )
    if not candidates:
        log.info("No in-progress consultations to check for catch-up")
        return

    for booking in candidates:
        try:
            process_booking(booking)
        except Exception:
            log.exception("Failed to process catch-up for booking %s — will retry next tick", booking["id"])


if __name__ == "__main__":
    db.init_db()
    run()
