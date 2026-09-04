"""Requirement 1: find consultations starting in about 15 minutes that
haven't been briefed yet, and message the manager's private Teams chat
with the client's name, the meeting's date/time, and a summary of what
the client answered on the Calendly booking form. That answer can be a
one-line note or a long, detailed narrative (case facts, dates, dollar
amounts, deadlines), so it's condensed with Groq to at most 6-7
sentences — see summarize_discussion_notes in app/summarizer.py, which
is built to preserve concrete facts and not pad out an already-short
answer. For repeat clients, a Groq-generated summary of their prior
consultation history is added underneath too. Run every 2 minutes via
cron (see deploy/crontab.txt).

Marking a booking "briefed" happens right after a delivery attempt, so
a flaky Teams call can't cause the same client to get double-briefed on
the next cron tick.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, leads_sheet, summarizer, teams_delivery
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _client_name_and_id(booking) -> tuple[str, str]:
    client_id = booking["client_id"]
    client = db.get_client(client_id)
    name = (client and (client.get("name") or client.get("email"))) or client_id
    return name, client_id


def process_booking(booking) -> None:
    client_name, client_id = _client_name_and_id(booking)

    # Condensed to at most 6-7 sentences with Groq — the client's raw
    # answer on the Calendly form (the "Please share anything that will
    # help prepare for our meeting" question) can be anywhere from one
    # line to a long case narrative, and the manager wants a consistently
    # short brief either way. Short answers are returned as-is rather
    # than padded out; see summarizer.summarize_discussion_notes.
    discussion_summary = summarizer.summarize_discussion_notes(client_name, booking["discussion_notes"])

    case_history = None
    if booking["is_repeat_client"]:
        prior_notes = [row["notes"] for row in db.get_prior_meeting_notes(client_id)]
        case_history = summarizer.summarize_client_history(client_name, prior_notes)

    manager_email = booking["attorney_email"] or "(unassigned — check Calendly event)"
    date_str, time_str = leads_sheet.format_date_time(booking["start_time"])

    posted = False
    if booking["attorney_email"]:
        manager_upn = settings.attorney_upn(booking["attorney_email"])
        lines = [
            f"Client: {client_name}",
            f"Date: {date_str}",
            f"Time: {time_str}",
            "",
            "What the client shared (summarized):",
            discussion_summary,
        ]
        if case_history:
            lines += ["", "Prior history (repeat client):", case_history]

        posted = teams_delivery.send_manager_message(
            manager_upn,
            title=f"Upcoming consultation: {client_name}",
            lines=lines,
        )
    else:
        log.error("Booking %s has no manager email — cannot message Teams", booking["id"])

    # Optional team-visible channel copy, never required.
    teams_delivery.post_channel_brief(
        client_name=client_name,
        attorney_email=manager_email,
        start_time_iso=booking["start_time"],
        discussion_summary=discussion_summary,
        case_history=case_history,
    )

    brief_summary = discussion_summary + (f"\n\n[Repeat client history]\n{case_history}" if case_history else "")
    db.mark_briefed(booking["id"], brief_summary)

    if posted:
        log.info("Briefed booking %s (%s) — messaged manager %s", booking["id"], client_name, manager_email)
    else:
        log.error(
            "Marked booking %s (%s) briefed, but the manager Teams message failed — "
            "check TEAMS_DM_ENABLED / tenant Graph consent (see README)",
            booking["id"],
            client_name,
        )


def run() -> None:
    due = db.get_unbriefed_bookings_in_window()
    if not due:
        log.info("No consultations due for briefing right now")
        return

    for booking in due:
        try:
            process_booking(booking)
        except Exception:
            log.exception("Failed to process booking %s — will retry on next tick", booking["id"])


if __name__ == "__main__":
    db.init_db()
    run()
