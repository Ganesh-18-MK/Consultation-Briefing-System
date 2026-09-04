from datetime import datetime, timedelta, timezone

from app import summarizer, db, fireflies_client, live_catchup, teams_delivery
from app.config import settings


def _make_in_progress_booking(temp_db, minutes_ago=15, join_url="https://teams.microsoft.com/fake"):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    booking_id = db.create_booking(
        "evt-live", client_id, "attorney@example.com", start.isoformat(), None, join_url, False
    )
    return booking_id


def test_pushes_summary_when_new_transcript_available(temp_db, monkeypatch):
    _make_in_progress_booking(temp_db)

    monkeypatch.setattr(
        fireflies_client, "get_live_transcript_text", lambda email, url: "Client: I need help with a visa. " * 20
    )
    monkeypatch.setattr(summarizer, "summarize_live_catchup", lambda name, text: "- discussed visa options")

    posted = []

    def fake_send(manager_upn, title, lines):
        posted.append((manager_upn, title, lines))
        return True

    monkeypatch.setattr(teams_delivery, "send_manager_message", fake_send)

    due = db.get_in_progress_bookings()
    assert len(due) == 1
    live_catchup.process_booking(due[0])

    assert len(posted) == 1
    manager_upn, title, lines = posted[0]
    assert manager_upn == "attorney@example.com"
    assert "Jane Client" in title
    assert lines == ["- discussed visa options"]

    booking = db.get_booking_by_uuid("evt-live")
    assert booking["catchup_push_count"] == 1
    assert booking["catchup_last_transcript_hash"]


def test_skips_when_no_transcript_yet(temp_db, monkeypatch):
    _make_in_progress_booking(temp_db)
    monkeypatch.setattr(fireflies_client, "get_live_transcript_text", lambda email, url: None)

    called = []
    monkeypatch.setattr(summarizer, "summarize_live_catchup", lambda name, text: called.append(1))

    due = db.get_in_progress_bookings()
    live_catchup.process_booking(due[0])

    assert called == []
    booking = db.get_booking_by_uuid("evt-live")
    assert booking["catchup_push_count"] == 0


def test_skips_when_transcript_too_short(temp_db, monkeypatch):
    _make_in_progress_booking(temp_db)
    monkeypatch.setattr(settings, "catchup_min_new_chars", 200)
    monkeypatch.setattr(fireflies_client, "get_live_transcript_text", lambda email, url: "short transcript")

    called = []
    monkeypatch.setattr(summarizer, "summarize_live_catchup", lambda name, text: called.append(1))

    due = db.get_in_progress_bookings()
    live_catchup.process_booking(due[0])

    assert called == []


def test_skips_duplicate_content_since_last_push(temp_db, monkeypatch):
    booking_id = _make_in_progress_booking(temp_db)
    transcript = "Client: discussing visa options at length. " * 20
    db.record_catchup_push(booking_id, live_catchup._hash(transcript))

    monkeypatch.setattr(fireflies_client, "get_live_transcript_text", lambda email, url: transcript)
    called = []
    monkeypatch.setattr(summarizer, "summarize_live_catchup", lambda name, text: called.append(1))

    booking = db.get_booking_by_uuid("evt-live")
    live_catchup.process_booking(booking)

    assert called == []


def test_records_push_even_when_teams_delivery_fails(temp_db, monkeypatch):
    _make_in_progress_booking(temp_db)
    monkeypatch.setattr(
        fireflies_client, "get_live_transcript_text", lambda email, url: "Client: I need help with a visa. " * 20
    )
    monkeypatch.setattr(summarizer, "summarize_live_catchup", lambda name, text: "- discussed visa options")
    monkeypatch.setattr(teams_delivery, "send_manager_message", lambda manager_upn, title, lines: False)

    due = db.get_in_progress_bookings()
    live_catchup.process_booking(due[0])

    booking = db.get_booking_by_uuid("evt-live")
    assert booking["catchup_push_count"] == 1


def test_get_in_progress_bookings_respects_push_cap(temp_db):
    booking_id = _make_in_progress_booking(temp_db)
    for _ in range(6):
        db.record_catchup_push(booking_id, "hash")

    due = db.get_in_progress_bookings(max_pushes=6)
    assert due == []


def test_get_in_progress_bookings_excludes_old_meetings(temp_db):
    _make_in_progress_booking(temp_db, minutes_ago=200)  # older than default 90-min window
    due = db.get_in_progress_bookings(max_duration_minutes=90)
    assert due == []


def test_get_in_progress_bookings_excludes_future_meetings(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    future_start = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.create_booking("evt-future", client_id, "attorney@example.com", future_start.isoformat(), None, "url", False)

    due = db.get_in_progress_bookings()
    assert due == []


def test_run_disabled_via_config(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "catchup_enabled", False)
    _make_in_progress_booking(temp_db)

    called = []
    monkeypatch.setattr(live_catchup, "process_booking", lambda b: called.append(b))
    live_catchup.run()

    assert called == []
