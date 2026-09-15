"""Firestore storage layer (Stage 2 in the plan: "Store and recognize
repeat clients"). Runs on Cloud Run alongside the webhook receiver — see
README's "Deploying to Cloud Run" section. Firestore (not a local file)
is what makes this safe to run on Cloud Run: Cloud Run's own disk is
wiped on every restart, so anything written to a local SQLite file or
Excel workbook would be lost. Firestore is Google's managed, persistent,
serverless database, free at this app's volume (see README).

Collections
-----------
clients        one doc per unique client, doc id = lowercased email
bookings       one doc per Calendly booking, doc id = calendly_event_uuid
                 (both IDs are already naturally unique, so no separate
                 auto-increment counter is needed — a lookup by email or
                 by event UUID is a direct document get, not a query)
meeting_notes  one doc per stored case-note summary (post-meeting or
                 backfill), auto-generated doc id — this is an
                 append-only log, not something looked up by a natural key

Query design note: several lookups here filter on a single field (e.g.
"all bookings for this client") rather than composing the full original
SQL WHERE clause in one Firestore query. That's deliberate — Firestore
auto-indexes every single field for free, but a query mixing more than
one equality/range condition across different fields needs a composite
index set up by hand. Where a result set is naturally small and
scoped (a client's own bookings, a booking's own notes), it's simpler
and just as cheap to do one narrow Firestore query and finish filtering
in Python. The two spots where that isn't true — the "briefing due
soon" and "in-progress meeting" scans, which have to search across ALL
bookings rather than one client's — do use a real composite index (on
`status` + `start_time`); see README's Cloud Run section for the exact
`gcloud firestore indexes composite create` command to run once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from google.cloud import firestore

from app.config import settings

CLIENTS = "clients"
BOOKINGS = "bookings"
MEETING_NOTES = "meeting_notes"

# Lazily-constructed singleton — real code never touches this directly,
# it always goes through _db(). Tests replace this with a fake client
# (see tests/fake_firestore.py) so pytest never needs real GCP
# credentials or network access.
_client_instance: Optional[firestore.Client] = None


def _db():
    global _client_instance
    if _client_instance is None:
        project = settings.gcp_project_id or None
        _client_instance = firestore.Client(project=project)
    return _client_instance


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc_to_dict(doc) -> Optional[dict]:
    """Firestore DocumentSnapshot -> plain dict, with the document ID
    folded in as "id" (matching how sqlite3.Row used to expose the
    primary key column) so every other module's `booking["id"]` /
    `client["id"]` style access keeps working unchanged."""
    if doc is None or not doc.exists:
        return None
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return data


def init_db() -> None:
    """No schema to create — Firestore collections/fields exist
    implicitly the moment a document is written to them. Kept as a
    no-op, callable function (rather than removed outright) purely so
    the existing callers — scripts/init_db.py, the webhook receiver's
    before_request safety net, run_webhook_server.py, and every test
    fixture — don't all need to change just because the storage engine
    changed underneath them."""
    return None


# ─── Clients ──────────────────────────────────────────────────────────

def upsert_client(email: str, name: Optional[str]) -> tuple[str, bool]:
    """Insert the client if new, otherwise update their name. Returns
    (client_id, is_new). client_id is the lowercased email — using it
    directly as the Firestore document ID is what makes "the same
    client email is always the same client" free, with no lookup
    query needed."""
    email = email.strip().lower()
    ref = _db().collection(CLIENTS).document(email)
    existing = ref.get()
    if existing.exists:
        if name:
            ref.update({"name": name})
        return email, False
    ref.set({"email": email, "name": name, "created_at": _now_iso()})
    return email, True


def get_client(client_id: str) -> Optional[dict]:
    return _doc_to_dict(_db().collection(CLIENTS).document(client_id).get())


def count_prior_bookings(client_id: str) -> int:
    # Scoped to one client's own bookings — naturally small regardless of
    # how much history the firm accumulates overall, so filtering out
    # canceled ones in Python (rather than a second Firestore condition)
    # stays cheap and needs no composite index.
    docs = (
        _db()
        .collection(BOOKINGS)
        .where("client_id", "==", client_id)
        .stream()
    )
    return sum(1 for d in docs if (d.to_dict() or {}).get("status") != "canceled")


# ─── Bookings ─────────────────────────────────────────────────────────

def create_booking(
    calendly_event_uuid: str,
    client_id: str,
    attorney_email: str,
    start_time_iso: str,
    discussion_notes: Optional[str],
    join_url: Optional[str],
    is_repeat_client: bool,
    brief_deadline_iso: Optional[str] = None,
) -> str:
    ref = _db().collection(BOOKINGS).document(calendly_event_uuid)
    ref.set(
        {
            "calendly_event_uuid": calendly_event_uuid,
            "client_id": client_id,
            "attorney_email": attorney_email,
            "start_time": start_time_iso,
            "status": "scheduled",
            "discussion_notes": discussion_notes,
            "join_url": join_url,
            "is_repeat_client": bool(is_repeat_client),
            # Requirement (2026-09-15): mam's two fixed daily consultation
            # blocks (see app/timezones.compute_brief_deadline_utc) each
            # have one shared briefing deadline rather than every booking
            # being briefed ~15 minutes before its own start time. None
            # here means the booking falls outside both blocks, and
            # get_unbriefed_bookings_in_window's original per-booking
            # window is the fallback for it (see that function below).
            "brief_deadline": brief_deadline_iso,
            "briefed_at": None,
            "brief_summary": None,
            "notes_synced_at": None,
            "catchup_push_count": 0,
            "catchup_last_pushed_at": None,
            "catchup_last_transcript_hash": None,
            "leads_sheet_synced_at": None,
            "created_at": _now_iso(),
        }
    )
    return calendly_event_uuid


def get_booking_by_uuid(calendly_event_uuid: str) -> Optional[dict]:
    return _doc_to_dict(_db().collection(BOOKINGS).document(calendly_event_uuid).get())


def cancel_booking(calendly_event_uuid: str) -> bool:
    ref = _db().collection(BOOKINGS).document(calendly_event_uuid)
    if not ref.get().exists:
        return False
    ref.update({"status": "canceled"})
    return True


def get_unbriefed_bookings_in_window(
    now: Optional[datetime] = None,
    lead_min_minutes: Optional[int] = None,
    lead_max_minutes: Optional[int] = None,
) -> list[dict]:
    """Stage 3 original design: consultations starting soon that haven't
    been briefed yet. Uses the (status, start_time) composite index —
    see README. Only a fallback now for a booking with no brief_deadline
    (i.e. one outside mam's two consultation blocks — see
    get_bookings_due_for_block_brief below, which is the primary path)."""
    now = now or datetime.now(timezone.utc)
    lead_min = lead_min_minutes if lead_min_minutes is not None else settings.brief_lead_min_minutes
    lead_max = lead_max_minutes if lead_max_minutes is not None else settings.brief_lead_max_minutes
    window_start = (now + timedelta(minutes=lead_min)).isoformat()
    window_end = (now + timedelta(minutes=lead_max)).isoformat()

    docs = (
        _db()
        .collection(BOOKINGS)
        .where("status", "==", "scheduled")
        .where("start_time", ">=", window_start)
        .where("start_time", "<=", window_end)
        .order_by("start_time")
        .stream()
    )
    results = [_doc_to_dict(d) for d in docs]
    return [b for b in results if b.get("briefed_at") is None and b.get("brief_deadline") is None]


def get_bookings_due_for_block_brief(now: Optional[datetime] = None) -> list[dict]:
    """Requirement (2026-09-15): the primary briefing path — every
    consultation whose block deadline (brief_deadline, set at booking
    time by app/timezones.compute_brief_deadline_utc) has arrived and
    hasn't been briefed yet. Needs its own (status, brief_deadline)
    composite index — see README — separate from the (status,
    start_time) one get_unbriefed_bookings_in_window above still uses
    for its fallback role."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()

    docs = (
        _db()
        .collection(BOOKINGS)
        .where("status", "==", "scheduled")
        .where("brief_deadline", "<=", now_iso)
        .order_by("brief_deadline")
        .stream()
    )
    results = [_doc_to_dict(d) for d in docs]
    return [b for b in results if b.get("briefed_at") is None]


def mark_briefed(booking_id: str, brief_summary: str) -> None:
    _db().collection(BOOKINGS).document(booking_id).update(
        {"briefed_at": _now_iso(), "brief_summary": brief_summary}
    )


# ─── Meeting notes / notes loop ────────────────────────────────────────

def get_prior_meeting_notes(client_id: str, limit: int = 5) -> list[dict]:
    """Most recent case-note summaries for a repeat client, newest
    first. A client's own note history is small, so this fetches by the
    single-field client_id index and sorts/limits in Python rather than
    needing a (client_id, created_at) composite index."""
    docs = (
        _db()
        .collection(MEETING_NOTES)
        .where("client_id", "==", client_id)
        .stream()
    )
    notes = sorted((_doc_to_dict(d) for d in docs), key=lambda n: n["created_at"], reverse=True)
    return notes[:limit]


def add_meeting_notes(booking_id: str, client_id: str, notes: str, source: str = "transcript") -> str:
    ref = _db().collection(MEETING_NOTES).document()
    ref.set(
        {
            "booking_id": booking_id,
            "client_id": client_id,
            "notes": notes,
            "source": source,
            "created_at": _now_iso(),
        }
    )
    return ref.id


def mark_notes_synced(booking_id: str) -> None:
    _db().collection(BOOKINGS).document(booking_id).update({"notes_synced_at": _now_iso()})


# ─── Live meeting catch-up (periodic push) ─────────────────────────────

def get_in_progress_bookings(
    now: Optional[datetime] = None,
    max_duration_minutes: int = 90,
    max_pushes: int = 6,
) -> list[dict]:
    """Bookings whose start_time has passed but are still assumed to be
    ongoing (within max_duration_minutes) and haven't hit the push cap
    yet. 'Assumed ongoing' because we have no reliable signal for when a
    Teams meeting actually ends short of polling Graph.

    Reuses the same (status, start_time) composite index as
    get_unbriefed_bookings_in_window — the start_time range here just
    looks backward instead of forward. join_url and catchup_push_count
    are filtered in Python: this result set is already bounded to a
    max_duration_minutes-wide slice of recent bookings, so it stays
    small and cheap regardless of the firm's total booking history."""
    now = now or datetime.now(timezone.utc)
    earliest_start = (now - timedelta(minutes=max_duration_minutes)).isoformat()
    now_iso = now.isoformat()

    docs = (
        _db()
        .collection(BOOKINGS)
        .where("status", "==", "scheduled")
        .where("start_time", ">=", earliest_start)
        .where("start_time", "<=", now_iso)
        .order_by("start_time")
        .stream()
    )
    results = [_doc_to_dict(d) for d in docs]
    return [
        b for b in results
        if b.get("join_url") is not None and (b.get("catchup_push_count") or 0) < max_pushes
    ]


def record_catchup_push(booking_id: str, transcript_hash: str) -> None:
    ref = _db().collection(BOOKINGS).document(booking_id)
    current = ref.get().to_dict() or {}
    ref.update(
        {
            "catchup_push_count": (current.get("catchup_push_count") or 0) + 1,
            "catchup_last_pushed_at": _now_iso(),
            "catchup_last_transcript_hash": transcript_hash,
        }
    )


# ─── Fireflies webhook correlation ─────────────────────────────────────

def get_booking_by_join_url(join_url: str) -> Optional[dict]:
    """Exact-match lookup used first when a Fireflies 'Transcription
    completed' webhook arrives. Restricted to bookings not yet
    notes-synced so an already-processed booking (or an unrelated old
    one that happens to share a stale join_url) doesn't match again. A
    given join URL is only ever used by a handful of bookings at most,
    so filtering notes_synced_at and sorting in Python (instead of a
    (join_url, notes_synced_at, start_time) composite index) is fine."""
    docs = (
        _db()
        .collection(BOOKINGS)
        .where("join_url", "==", join_url)
        .stream()
    )
    candidates = [_doc_to_dict(d) for d in docs if (d.to_dict() or {}).get("notes_synced_at") is None]
    if not candidates:
        return None
    candidates.sort(key=lambda b: b["start_time"], reverse=True)
    return candidates[0]


def get_pending_notes_bookings(organizer_email: str, since_hours: int = 12) -> list[dict]:
    """Fallback candidates for Fireflies webhook correlation when the
    join_url doesn't match exactly (URL formatting can drift between
    what Calendly stored and what Fireflies recorded) — recent,
    not-yet-synced bookings for the same organizer, for a normalized-URL
    or nearest-start-time match in Python. Uses the (attorney_email,
    notes_synced_at) composite index — see README."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    docs = (
        _db()
        .collection(BOOKINGS)
        .where("attorney_email", "==", organizer_email)
        .where("notes_synced_at", "==", None)
        .stream()
    )
    results = [_doc_to_dict(d) for d in docs]
    recent = [b for b in results if b["start_time"] >= cutoff]
    recent.sort(key=lambda b: b["start_time"], reverse=True)
    return recent


# ─── Leads sheet sync tracking ─────────────────────────────────────────

def mark_leads_sheet_synced(booking_id: str) -> None:
    _db().collection(BOOKINGS).document(booking_id).update({"leads_sheet_synced_at": _now_iso()})


def get_bookings_needing_leads_sheet_sync() -> list[dict]:
    """Bookings never appended to the leads sheet — used both by the
    webhook's immediate append (marks itself synced right after) and by
    a manual backfill pass. In normal operation this stays empty or
    near-empty (bookings sync immediately), so fetching each booking's
    client doc individually here is fine."""
    docs = (
        _db()
        .collection(BOOKINGS)
        .where("leads_sheet_synced_at", "==", None)
        .stream()
    )
    results = [_doc_to_dict(d) for d in docs]
    for booking in results:
        client = get_client(booking["client_id"]) or {}
        booking["client_name"] = client.get("name")
        booking["client_email"] = client.get("email")
    results.sort(key=lambda b: b["start_time"])
    return results
