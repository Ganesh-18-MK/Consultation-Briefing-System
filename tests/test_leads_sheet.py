from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import Workbook, load_workbook

from app import db, leads_sheet
from app.config import settings


def _make_booking(start_iso=None):
    client_id, _ = db.upsert_client("jane@example.com", "Jane Client")
    start = start_iso or (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    booking_id = db.create_booking(
        "evt-1", client_id, "attorney@example.com", start, "Wants an H-1B extension", None, False
    )
    return booking_id


def test_format_date_time_splits_iso_string():
    date_str, time_str = leads_sheet.format_date_time("2026-09-10T15:30:00+00:00")
    assert date_str == "2026-09-10"
    assert time_str == "15:30 UTC"


def test_format_date_time_handles_missing_value():
    assert leads_sheet.format_date_time(None) == ("", "")


def test_format_date_time_handles_unparseable_value():
    date_str, time_str = leads_sheet.format_date_time("not-a-date")
    assert date_str == "not-a-date"
    assert time_str == ""


def test_append_creates_default_template_when_missing(tmp_path, monkeypatch):
    dest = tmp_path / "subdir" / "leads.xlsx"
    monkeypatch.setattr(settings, "leads_sheet_path", str(dest))
    monkeypatch.setattr(settings, "leads_sheet_enabled", True)

    leads_sheet.append_booking_row("2026-09-10", "15:30 UTC", "Jane Client", "H-1B extension")

    assert dest.exists()
    wb = load_workbook(dest)
    ws = wb.active
    header_row = [cell.value for cell in ws[1]]
    assert header_row == leads_sheet.DEFAULT_HEADERS
    data_row = [cell.value for cell in ws[2]]
    assert data_row == ["2026-09-10", "15:30 UTC", "Jane Client", "H-1B extension"]
    assert list(dest.parent.glob("*.tmp")) == []


def test_append_matches_existing_headers_by_alias(tmp_path, monkeypatch):
    dest = tmp_path / "manager_template.xlsx"
    wb = Workbook()
    ws = wb.active
    # Manager's own header wording/order — not the app's default.
    ws.append(["Consultation Purpose", "Client", "Date", "Time of Meeting"])
    wb.save(dest)

    monkeypatch.setattr(settings, "leads_sheet_path", str(dest))
    monkeypatch.setattr(settings, "leads_sheet_enabled", True)

    leads_sheet.append_booking_row("2026-09-10", "15:30 UTC", "Jane Client", "H-1B extension")

    wb2 = load_workbook(dest)
    ws2 = wb2.active
    data_row = [cell.value for cell in ws2[2]]
    assert data_row == ["H-1B extension", "Jane Client", "2026-09-10", "15:30 UTC"]


def test_append_multiple_rows_accumulate(tmp_path, monkeypatch):
    dest = tmp_path / "leads.xlsx"
    monkeypatch.setattr(settings, "leads_sheet_path", str(dest))
    monkeypatch.setattr(settings, "leads_sheet_enabled", True)

    leads_sheet.append_booking_row("2026-09-10", "15:30 UTC", "Jane Client", "H-1B extension")
    leads_sheet.append_booking_row("2026-09-11", "09:00 UTC", "John Other", "Green card")

    wb = load_workbook(dest)
    ws = wb.active
    assert ws.max_row == 3
    assert [cell.value for cell in ws[3]] == ["2026-09-11", "09:00 UTC", "John Other", "Green card"]


def test_append_skips_when_disabled(tmp_path, monkeypatch):
    dest = tmp_path / "leads.xlsx"
    monkeypatch.setattr(settings, "leads_sheet_path", str(dest))
    monkeypatch.setattr(settings, "leads_sheet_enabled", False)

    leads_sheet.append_booking_row("2026-09-10", "15:30 UTC", "Jane Client", "H-1B extension")

    assert not dest.exists()


def test_backfill_appends_and_marks_synced(temp_db, tmp_path, monkeypatch):
    dest = tmp_path / "leads.xlsx"
    monkeypatch.setattr(settings, "leads_sheet_path", str(dest))
    monkeypatch.setattr(settings, "leads_sheet_enabled", True)

    booking_id = _make_booking()

    leads_sheet.backfill_missing_rows()

    wb = load_workbook(dest)
    assert wb.active.max_row == 2

    pending = db.get_bookings_needing_leads_sheet_sync()
    assert pending == []
    # Re-running should not duplicate the row.
    leads_sheet.backfill_missing_rows()
    wb2 = load_workbook(dest)
    assert wb2.active.max_row == 2


def test_backfill_noop_when_nothing_pending(temp_db, tmp_path, monkeypatch):
    dest = tmp_path / "leads.xlsx"
    monkeypatch.setattr(settings, "leads_sheet_path", str(dest))
    monkeypatch.setattr(settings, "leads_sheet_enabled", True)

    leads_sheet.backfill_missing_rows()

    assert not dest.exists()
