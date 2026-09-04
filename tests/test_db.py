from datetime import datetime, timedelta, timezone

from app import db


def test_upsert_client_creates_then_reuses(temp_db):
    client_id_1, is_new_1 = db.upsert_client("jane@example.com", "Jane Client")
    client_id_2, is_new_2 = db.upsert_client("JANE@example.com", "Jane C.")  # case-insensitive

    assert is_new_1 is True
    assert is_new_2 is False
    assert client_id_1 == client_id_2


def test_repeat_client_detection(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    assert db.count_prior_bookings(client_id) == 0

    db.create_booking(
        calendly_event_uuid="evt-1",
        client_id=client_id,
        attorney_email="attorney@example.com",
        start_time_iso=datetime.now(timezone.utc).isoformat(),
        discussion_notes="First visit",
        join_url=None,
        is_repeat_client=False,
    )
    assert db.count_prior_bookings(client_id) == 1

    # A second booking for the same client should now be flagged repeat.
    is_repeat = db.count_prior_bookings(client_id) > 0
    assert is_repeat is True


def test_canceled_booking_excluded_from_repeat_count(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    db.create_booking(
        calendly_event_uuid="evt-1",
        client_id=client_id,
        attorney_email="attorney@example.com",
        start_time_iso=datetime.now(timezone.utc).isoformat(),
        discussion_notes=None,
        join_url=None,
        is_repeat_client=False,
    )
    db.cancel_booking("evt-1")
    assert db.count_prior_bookings(client_id) == 0


def test_unbriefed_bookings_window(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    now = datetime.now(timezone.utc)

    in_window = now + timedelta(minutes=10)
    too_soon = now + timedelta(minutes=1)
    too_far = now + timedelta(minutes=30)

    db.create_booking("evt-in-window", client_id, "attorney@example.com", in_window.isoformat(), None, None, False)
    db.create_booking("evt-too-soon", client_id, "attorney@example.com", too_soon.isoformat(), None, None, False)
    db.create_booking("evt-too-far", client_id, "attorney@example.com", too_far.isoformat(), None, None, False)

    due = db.get_unbriefed_bookings_in_window(now=now, lead_min_minutes=8, lead_max_minutes=13)
    uuids = {row["calendly_event_uuid"] for row in due}

    assert uuids == {"evt-in-window"}


def test_mark_briefed_removes_booking_from_window(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=10)
    booking_id = db.create_booking("evt-1", client_id, "attorney@example.com", start.isoformat(), None, None, False)

    db.mark_briefed(booking_id, "brief text")

    due = db.get_unbriefed_bookings_in_window(now=now, lead_min_minutes=8, lead_max_minutes=13)
    assert due == []


def test_meeting_notes_ordering(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    booking_id = db.create_booking(
        "evt-1", client_id, "attorney@example.com", datetime.now(timezone.utc).isoformat(), None, None, False
    )
    db.add_meeting_notes(booking_id, client_id, "First meeting notes")
    db.add_meeting_notes(booking_id, client_id, "Second meeting notes")

    notes = db.get_prior_meeting_notes(client_id)
    assert [n["notes"] for n in notes] == ["Second meeting notes", "First meeting notes"]


def test_get_booking_by_join_url_excludes_already_synced(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    booking_id = db.create_booking(
        "evt-1", client_id, "attorney@example.com", datetime.now(timezone.utc).isoformat(),
        None, "https://teams.microsoft.com/l/meetup-join/abc", False,
    )

    found = db.get_booking_by_join_url("https://teams.microsoft.com/l/meetup-join/abc")
    assert found["calendly_event_uuid"] == "evt-1"

    db.mark_notes_synced(booking_id)
    assert db.get_booking_by_join_url("https://teams.microsoft.com/l/meetup-join/abc") is None


def test_get_pending_notes_bookings_filters_by_organizer_and_recency(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    now = datetime.now(timezone.utc)

    db.create_booking("evt-recent", client_id, "manager@example.com", now.isoformat(), None, "url1", False)
    db.create_booking(
        "evt-old", client_id, "manager@example.com", (now - timedelta(hours=48)).isoformat(), None, "url2", False
    )
    db.create_booking(
        "evt-other-org", client_id, "someone-else@example.com", now.isoformat(), None, "url3", False
    )

    pending = db.get_pending_notes_bookings("manager@example.com", since_hours=12)
    uuids = {row["calendly_event_uuid"] for row in pending}
    assert uuids == {"evt-recent"}


def test_leads_sheet_sync_tracking(temp_db):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    booking_id = db.create_booking(
        "evt-1", client_id, "attorney@example.com", datetime.now(timezone.utc).isoformat(), None, None, False
    )

    pending = db.get_bookings_needing_leads_sheet_sync()
    assert len(pending) == 1
    assert pending[0]["client_name"] == "Jane Client"

    db.mark_leads_sheet_synced(booking_id)
    assert db.get_bookings_needing_leads_sheet_sync() == []
