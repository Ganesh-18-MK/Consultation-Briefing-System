from datetime import datetime, timedelta, timezone

from app import brief_scheduler, leads_sheet, summarizer, db, teams_delivery

# Captured at import time, before any per-test autouse mocking of
# summarizer.summarize_discussion_notes (see conftest.py) rebinds the
# module attribute — lets a specific test restore the real function.
_real_summarize_discussion_notes = summarizer.summarize_discussion_notes


def test_brief_uses_groq_summary_of_the_clients_answer(temp_db, monkeypatch):
    """Requirement 1: the client's raw Calendly answer can be one line
    or a long case narrative, so it's condensed via Groq to a short
    bullet-point list before it's sent to the manager."""
    monkeypatch.setattr(
        summarizer, "summarize_discussion_notes",
        lambda name, notes: "- condensed to a few sentences, key facts preserved",
    )

    dm_calls = []

    def fake_send(manager_upn, title, lines):
        dm_calls.append((manager_upn, title, lines))
        return True

    monkeypatch.setattr(teams_delivery, "send_manager_message", fake_send)
    monkeypatch.setattr(teams_delivery, "post_channel_brief", lambda *a, **k: False)

    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    long_answer = (
        "Counsel demands $100,000 by Sept 14, 2026 over withdrawal from a contractor "
        "placement. Signed NDAs Apr 11, refused to commit Apr 17."
    )
    booking_id = db.create_booking(
        "evt-1", client_id, "attorney@example.com", start.isoformat(), long_answer, None, False,
    )

    due = db.get_unbriefed_bookings_in_window()
    assert len(due) == 1
    brief_scheduler.process_booking(due[0])

    assert len(dm_calls) == 1
    manager_upn, title, lines = dm_calls[0]
    assert manager_upn == "attorney@example.com"
    assert "Jane Client" in title
    joined = "\n".join(lines)
    assert "condensed to a few sentences, key facts preserved" in joined
    assert "What the client shared (summarized):" in joined
    # The raw, un-summarized answer should NOT appear verbatim in the message.
    assert long_answer not in joined
    assert "Prior history" not in joined

    booking = db.get_booking_by_uuid("evt-1")
    assert booking["briefed_at"] is not None
    assert booking["id"] == booking_id


def test_brief_includes_client_name_date_and_time(temp_db, monkeypatch):
    monkeypatch.setattr(summarizer, "summarize_discussion_notes", lambda name, notes: "Some purpose, summarized.")
    dm_calls = []
    monkeypatch.setattr(
        teams_delivery, "send_manager_message",
        lambda manager_upn, title, lines: (dm_calls.append(lines), True)[1],
    )
    monkeypatch.setattr(teams_delivery, "post_channel_brief", lambda *a, **k: False)

    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.create_booking("evt-2", client_id, "attorney@example.com", start.isoformat(), "Some purpose", None, False)

    due = db.get_unbriefed_bookings_in_window()
    brief_scheduler.process_booking(due[0])

    lines = dm_calls[0]
    joined = "\n".join(lines)
    assert "Client: Jane Client" in joined
    # Compare against the same conversion the app itself uses (UTC -> IST)
    # rather than recomputing it here — a UTC-vs-IST date rollover near
    # midnight would otherwise make this assertion flaky depending on
    # when the test happens to run.
    expected_date, expected_time = leads_sheet.format_date_time(start.isoformat())
    assert f"Date: {expected_date}" in joined
    assert f"Time: {expected_time}" in joined


def test_brief_handles_missing_answer_gracefully(temp_db, monkeypatch):
    # summarize_discussion_notes itself handles the "no answer" case —
    # restoring the real function (over the autouse mock in conftest.py)
    # so the real short-circuit, no-Groq-call behavior is exercised for
    # a None discussion_notes.
    monkeypatch.setattr(summarizer, "summarize_discussion_notes", _real_summarize_discussion_notes)
    dm_calls = []
    monkeypatch.setattr(
        teams_delivery, "send_manager_message",
        lambda manager_upn, title, lines: (dm_calls.append(lines), True)[1],
    )
    monkeypatch.setattr(teams_delivery, "post_channel_brief", lambda *a, **k: False)

    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.create_booking("evt-3", client_id, "attorney@example.com", start.isoformat(), None, None, False)

    due = db.get_unbriefed_bookings_in_window()
    brief_scheduler.process_booking(due[0])

    joined = "\n".join(dm_calls[0])
    assert "No answer was provided" in joined


def test_repeat_client_brief_includes_history_in_manager_dm(temp_db, monkeypatch):
    monkeypatch.setattr(summarizer, "summarize_discussion_notes", lambda name, notes: "- new topic, summarized")
    monkeypatch.setattr(summarizer, "summarize_client_history", lambda name, notes: "Prior case: visa consult.")

    dm_calls = []

    def fake_send(manager_upn, title, lines):
        dm_calls.append((manager_upn, title, lines))
        return True

    monkeypatch.setattr(teams_delivery, "send_manager_message", fake_send)
    monkeypatch.setattr(teams_delivery, "post_channel_brief", lambda *a, **k: False)

    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    past_start = datetime.now(timezone.utc) - timedelta(days=30)
    first_booking_id = db.create_booking("evt-old", client_id, "attorney@example.com", past_start.isoformat(), "old notes", None, False)
    db.add_meeting_notes(first_booking_id, client_id, "Discussed initial filing.")

    new_start = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.create_booking("evt-new", client_id, "attorney@example.com", new_start.isoformat(), "follow-up notes", None, True)

    due = [b for b in db.get_unbriefed_bookings_in_window() if b["calendly_event_uuid"] == "evt-new"]
    assert len(due) == 1
    brief_scheduler.process_booking(due[0])

    assert len(dm_calls) == 1
    manager_upn, title, lines = dm_calls[0]
    assert manager_upn == "attorney@example.com"
    joined = "\n".join(lines)
    assert "- new topic, summarized" in joined
    assert "Prior case: visa consult." in joined

    booking = db.get_booking_by_uuid("evt-new")
    assert booking["briefed_at"] is not None
    assert "Prior case" in booking["brief_summary"]


def test_marks_briefed_even_when_manager_dm_fails(temp_db, monkeypatch):
    monkeypatch.setattr(summarizer, "summarize_discussion_notes", lambda name, notes: "summary")
    monkeypatch.setattr(teams_delivery, "send_manager_message", lambda manager_upn, title, lines: False)
    monkeypatch.setattr(teams_delivery, "post_channel_brief", lambda *a, **k: False)

    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.create_booking("evt-1", client_id, "attorney@example.com", start.isoformat(), "raw notes", None, False)

    due = db.get_unbriefed_bookings_in_window()
    brief_scheduler.process_booking(due[0])

    booking = db.get_booking_by_uuid("evt-1")
    assert booking["briefed_at"] is not None


def test_logs_error_when_no_manager_email(temp_db, monkeypatch):
    monkeypatch.setattr(summarizer, "summarize_discussion_notes", lambda name, notes: "summary")
    dm_calls = []
    monkeypatch.setattr(teams_delivery, "send_manager_message", lambda *a, **k: dm_calls.append(1))
    monkeypatch.setattr(teams_delivery, "post_channel_brief", lambda *a, **k: False)

    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.create_booking("evt-1", client_id, "", start.isoformat(), "raw notes", None, False)

    due = db.get_unbriefed_bookings_in_window()
    brief_scheduler.process_booking(due[0])

    assert dm_calls == []
    booking = db.get_booking_by_uuid("evt-1")
    assert booking["briefed_at"] is not None


def test_run_skips_when_nothing_due(temp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(brief_scheduler, "process_booking", lambda b: calls.append(b))
    brief_scheduler.run()
    assert calls == []


def test_run_continues_after_one_booking_fails(temp_db, monkeypatch):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.create_booking("evt-a", client_id, "attorney@example.com", start.isoformat(), None, None, False)
    db.create_booking("evt-b", client_id, "attorney@example.com", start.isoformat(), None, None, False)

    processed = []

    def fake_process(booking):
        if booking["calendly_event_uuid"] == "evt-a":
            raise RuntimeError("boom")
        processed.append(booking["calendly_event_uuid"])

    monkeypatch.setattr(brief_scheduler, "process_booking", fake_process)
    brief_scheduler.run()

    assert processed == ["evt-b"]
