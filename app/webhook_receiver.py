"""The always-on webhook receiver — now handling two independent event
sources:

- POST /webhooks/calendly — Stage 1 & 2 of the plan: booking/cancel
  events, stored in the DB, repeat-client flagging, and (Requirement 5)
  an immediate row appended to the leads spreadsheet.
- POST /webhooks/fireflies — fires when Fireflies.ai finishes
  transcribing a captured consultation (Requirement 2's capture and
  Requirement 4's post-meeting notes). Matches the finished transcript
  back to a booking, summarizes it, stores it, and messages the manager.

Also exposes two internal, shared-secret-protected trigger endpoints so
the two time-based jobs (Requirement 1's brief scheduler, Requirement 3's
live catch-up) can run on this same Cloud Run service, called on a
schedule by Cloud Scheduler instead of cron:

- POST /internal/trigger/brief-scheduler
- POST /internal/trigger/live-catchup

Deployed as a single Cloud Run service — see README's "Deploying to
Cloud Run" section and deploy/cloud_run.md.
"""
from __future__ import annotations

import hmac
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify

from app import brief_scheduler, calendly_client, db, fireflies_client, leads_sheet, live_catchup, summarizer, teams_delivery, timezones
from app.calendly_signature import SignatureError, verify_signature
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

app = Flask(__name__)


@app.before_request
def _ensure_db_ready() -> None:
    # Idempotent (CREATE TABLE IF NOT EXISTS + a migration check), so
    # running it on every request is cheap. A safety net on top of the
    # documented `python scripts/init_db.py` one-time setup step — if
    # that step is ever skipped or the DB file goes missing, the first
    # webhook or scheduled tick just recreates the schema instead of
    # erroring out.
    db.init_db()


def _client_name(client_id: str) -> str:
    client = db.get_client(client_id)
    return (client and (client.get("name") or client.get("email"))) or client_id


def _fmt_dt(iso_str: str | None) -> str:
    """Post-meeting-notes Teams message uses this — kept consistent with
    the pre-meeting brief's Date/Time formatting (leads_sheet.format_date_time),
    which converts to IST for display."""
    if not iso_str:
        return ""
    try:
        date_str, time_str = leads_sheet.format_date_time(iso_str)
        return f"{date_str} {time_str}"
    except ValueError:
        return iso_str


# ─── Calendly ────────────────────────────────────────────────────────

def _extract_discussion_notes(payload: dict) -> str | None:
    """Pull the client's answer to the required discussion-topic question.
    Falls back to concatenating every question/answer pair if the
    configured question text doesn't match exactly (booking forms get
    edited; this keeps a webhook change from silently dropping data)."""
    qas = payload.get("questions_and_answers") or []
    target = settings.calendly_discussion_question.strip().lower()

    for qa in qas:
        if (qa.get("question") or "").strip().lower() == target:
            return qa.get("answer")

    if qas:
        log.warning(
            "Discussion question %r not found verbatim in payload; "
            "falling back to all Q&A pairs",
            settings.calendly_discussion_question,
        )
        return "\n".join(f"{qa.get('question')}: {qa.get('answer')}" for qa in qas)

    return None


def _handle_invitee_created(payload: dict) -> tuple[dict, int]:
    booking_uuid = calendly_client.extract_uuid(payload["uri"])

    if db.get_booking_by_uuid(booking_uuid):
        log.info("Booking %s already recorded; ignoring duplicate delivery", booking_uuid)
        return {"status": "duplicate"}, 200

    client_email = payload["email"]
    client_name = payload.get("name")
    discussion_notes = _extract_discussion_notes(payload)
    # Summarized immediately (not just later, at brief time) so the
    # leads sheet gets the same short numbered-list version the manager
    # sees in Teams, instead of the client's raw, unformatted answer.
    discussion_summary_for_sheet = summarizer.summarize_discussion_notes(client_name or client_email, discussion_notes)

    event_details = calendly_client.get_event_details(payload["event"])

    client_id, _is_new = db.upsert_client(client_email, client_name)
    is_repeat_client = db.count_prior_bookings(client_id) > 0

    start_time = event_details["start_time"]
    if not start_time:
        # Without a start_time the brief scheduler can never pick this
        # booking up. Record it anyway (for the repeat-client count and
        # manual follow-up) but make the gap loud.
        log.error(
            "No start_time resolved for booking %s (client %s) — check "
            "CALENDLY_API_TOKEN and the event location type",
            booking_uuid,
            client_email,
        )

    booking_id = db.create_booking(
        calendly_event_uuid=booking_uuid,
        client_id=client_id,
        attorney_email=event_details["attorney_email"] or "",
        start_time_iso=start_time or "",
        discussion_notes=discussion_notes,
        join_url=event_details["join_url"],
        is_repeat_client=is_repeat_client,
        brief_deadline_iso=timezones.compute_brief_deadline_utc(start_time),
    )

    log.info(
        "Recorded booking %s (id=%s) for %s — repeat_client=%s",
        booking_uuid,
        booking_id,
        client_email,
        is_repeat_client,
    )

    # Requirement 5: one row per CLIENT (keyed by email), one column per
    # distinct booking date — written the moment the booking is made,
    # never blocking recording the booking itself if the sheet write
    # fails. A failure here just means the booking stays in
    # get_bookings_needing_leads_sheet_sync() for the manual --backfill
    # pass to pick up later, rather than being lost.
    try:
        date_str = leads_sheet.format_date(start_time)
        leads_sheet.record_client_response(
            client_name=client_name or client_email,
            client_email=client_email,
            date_str=date_str,
            questions=discussion_summary_for_sheet,
        )
        db.mark_leads_sheet_synced(booking_id)
    except Exception:
        log.exception("Failed to record leads sheet entry for booking %s", booking_uuid)

    return {"status": "recorded", "booking_id": booking_id}, 201


def _handle_invitee_canceled(payload: dict) -> tuple[dict, int]:
    booking_uuid = calendly_client.extract_uuid(payload["uri"])
    found = db.cancel_booking(booking_uuid)
    if found:
        log.info("Canceled booking %s", booking_uuid)
        return {"status": "canceled"}, 200
    log.warning("Cancel received for unknown booking %s", booking_uuid)
    return {"status": "unknown_booking"}, 200


CALENDLY_HANDLERS = {
    "invitee.created": _handle_invitee_created,
    "invitee.canceled": _handle_invitee_canceled,
}


@app.route("/webhooks/calendly", methods=["POST"])
def calendly_webhook():
    raw_body = request.get_data()

    try:
        verify_signature(
            raw_body,
            request.headers.get("Calendly-Webhook-Signature"),
            settings.calendly_signing_secret,
        )
    except SignatureError as exc:
        log.warning("Rejected Calendly webhook: %s", exc)
        return jsonify({"error": str(exc)}), 401

    body = request.get_json(silent=True) or {}
    event = body.get("event")
    payload = body.get("payload") or {}

    handler = CALENDLY_HANDLERS.get(event)
    if handler is None:
        log.info("Ignoring unhandled Calendly event type: %s", event)
        return jsonify({"status": "ignored", "event": event}), 200

    try:
        result, status = handler(payload)
    except KeyError as exc:
        log.error("Malformed payload for event %s, missing field %s", event, exc)
        return jsonify({"error": f"missing field {exc}"}), 400
    except Exception:
        log.exception("Unhandled error processing Calendly webhook (event=%s)", event)
        return jsonify({"error": "internal error"}), 500

    return jsonify(result), status


# ─── Fireflies ───────────────────────────────────────────────────────

def _find_booking_for_transcript(transcript: dict) -> dict | None:
    """Correlates a finished Fireflies transcript back to one of our
    bookings. Tries an exact join_url match first, then falls back to
    the closest not-yet-synced booking for the same organizer within a
    recent window — URL formatting can drift slightly between what
    Calendly stored and what Fireflies recorded."""
    join_url = transcript.get("meeting_link") or ""
    booking = db.get_booking_by_join_url(join_url)
    if booking:
        return booking

    organizer_email = transcript.get("organizer_email") or ""
    if not organizer_email:
        return None

    target = fireflies_client.normalize_join_url(join_url)
    candidates = db.get_pending_notes_bookings(organizer_email)

    if target:
        for candidate in candidates:
            if fireflies_client.normalize_join_url(candidate["join_url"]) == target:
                return candidate

    # Last resort: nearest booking by start_time for this organizer.
    return candidates[0] if candidates else None


def _process_fireflies_transcript(meeting_id: str) -> None:
    transcript = fireflies_client.get_transcript(meeting_id)
    if not transcript:
        log.warning("Fireflies reported meeting %s complete but transcript fetch returned nothing", meeting_id)
        return

    booking = _find_booking_for_transcript(transcript)
    if not booking:
        log.warning(
            "Could not match Fireflies meeting %s (link=%s, organizer=%s) to any pending booking",
            meeting_id,
            transcript.get("meeting_link"),
            transcript.get("organizer_email"),
        )
        return

    client_name = _client_name(booking["client_id"])
    text = fireflies_client.transcript_text(transcript)

    if not text.strip():
        log.warning("Empty Fireflies transcript for booking %s — marking synced with no notes", booking["id"])
        db.mark_notes_synced(booking["id"])
        return

    case_notes = summarizer.summarize_transcript(client_name, text)
    db.add_meeting_notes(booking["id"], booking["client_id"], case_notes, source="fireflies")
    db.mark_notes_synced(booking["id"])

    manager_upn = settings.attorney_upn(booking["attorney_email"]) if booking["attorney_email"] else None
    if manager_upn:
        teams_delivery.send_manager_message(
            manager_upn,
            title=f"Meeting notes: {client_name}",
            lines=[
                f"Client: {client_name}",
                f"Date: {_fmt_dt(booking['start_time'])}",
                f"Purpose: {booking['discussion_notes'] or '(not provided)'}",
                "",
                case_notes,
            ],
        )

    log.info("Stored and delivered post-meeting notes for booking %s (%s)", booking["id"], client_name)


# The exact wording Fireflies uses for "transcript is ready" has moved
# around between their docs and their actual product UI — the live
# webhook config screen offers "Meeting Transcribed" where older docs
# said "Transcription completed". Rather than hardcode one exact string
# (and silently ignore every real event if it's ever slightly different
# again), match case-insensitively against every wording seen so far.
# If a real event still doesn't match, it'll show up in the logs below
# with its exact eventType value — add it to this set.
TRANSCRIPT_READY_EVENT_TYPES = {
    "transcription completed",
    "meeting transcribed",
}


@app.route("/webhooks/fireflies", methods=["POST"])
def fireflies_webhook():
    raw_body = request.get_data()

    if not fireflies_client.verify_webhook_signature(
        raw_body, request.headers.get("x-hub-signature"), settings.fireflies_webhook_secret
    ):
        log.warning("Rejected Fireflies webhook: signature mismatch")
        return jsonify({"error": "invalid signature"}), 401

    body = request.get_json(silent=True) or {}
    event_type = body.get("eventType")
    meeting_id = body.get("meetingId")

    is_ready = (event_type or "").strip().lower() in TRANSCRIPT_READY_EVENT_TYPES
    if not is_ready or not meeting_id:
        log.info("Ignoring Fireflies webhook (eventType=%r)", event_type)
        return jsonify({"status": "ignored"}), 200

    try:
        _process_fireflies_transcript(meeting_id)
    except Exception:
        log.exception("Unhandled error processing Fireflies webhook for meeting %s", meeting_id)
        return jsonify({"error": "internal error"}), 500

    return jsonify({"status": "processed"}), 200


# ─── Internal scheduled-job triggers (Cloud Run) ────────────────────
#
# Cloud Scheduler calls these on a schedule in place of cron — see
# deploy/cloud_run.md for the exact `gcloud scheduler jobs create` commands.
# Guarded by a shared secret so the URLs can't be triggered by anyone who
# finds them; if INTERNAL_TRIGGER_SECRET is unset, both routes refuse
# every request rather than silently allowing unauthenticated triggers.

def _require_internal_secret(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        configured = settings.internal_trigger_secret
        provided = request.headers.get("X-Internal-Trigger-Secret", "")
        if not configured or not hmac.compare_digest(configured, provided):
            log.warning("Rejected internal trigger call to %s: bad or missing secret", request.path)
            return jsonify({"error": "unauthorized"}), 401
        return handler(*args, **kwargs)

    return wrapped


@app.route("/internal/trigger/brief-scheduler", methods=["POST"])
@_require_internal_secret
def trigger_brief_scheduler():
    try:
        brief_scheduler.run()
    except Exception:
        log.exception("Unhandled error running brief_scheduler.run() via internal trigger")
        return jsonify({"error": "internal error"}), 500
    return jsonify({"status": "ok"}), 200


@app.route("/internal/trigger/live-catchup", methods=["POST"])
@_require_internal_secret
def trigger_live_catchup():
    try:
        live_catchup.run()
    except Exception:
        log.exception("Unhandled error running live_catchup.run() via internal trigger")
        return jsonify({"error": "internal error"}), 500
    return jsonify({"status": "ok"}), 200


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    db.init_db()
    app.run(host=settings.webhook_host, port=settings.webhook_port)
