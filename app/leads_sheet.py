"""Requirement 5: one row per Calendly booking in a shared-drive Excel
file — Date, Time, Client Name, Purpose — appended the moment each
booking comes in (called from webhook_receiver.py), not rebuilt on a
schedule.

This is designed around the manager's own template file (columns
already set up their way) rather than a file this code owns: point
LEADS_SHEET_PATH at it and rows get matched to its existing headers by
name, case-insensitively, wherever they are. If nothing exists at that
path yet, a minimal default template is created automatically so the
system works before the real file is dropped in — swap in the real one
at the same path whenever it's ready and run `python -m app.leads_sheet
--backfill` (see below) to add every booking made in the meantime.
"""
from __future__ import annotations

import fcntl
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook, load_workbook

from app import db
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

DEFAULT_HEADERS = ["Date", "Time", "Client Name", "Purpose"]

# Case-insensitive substring match against the sheet's actual header row,
# tried in order — first match wins. Covers the wording in the request
# ("date", "time of meeting", "client name", "purpose") plus common
# variations, since it's the manager's own file, not one this code wrote.
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["date"],
    "time": ["time"],
    "client_name": ["client name", "client", "name"],
    "purpose": ["purpose", "consultation purpose", "topic", "discussion", "reason for"],
}


@contextmanager
def _locked(path: Path):
    """A simple cross-process advisory lock so two near-simultaneous
    bookings can't read-modify-write the same file at once and stomp on
    each other's new row. On a real filesystem (the VM path, or local
    dev) this is a hard guarantee via flock(). On Cloud Run this file
    instead lives on a mounted Cloud Storage bucket (see README's
    "Deploying to Cloud Run" section) so it survives restarts — but
    Cloud Storage's FUSE layer doesn't implement real POSIX file
    locking, so flock() there can raise OSError. That's fine to fall
    back on rather than crash: the Cloud Run deployment already runs a
    single worker on a single instance (see Dockerfile / README), so
    requests are handled one at a time regardless — this lock is
    defense-in-depth there, not the only thing preventing a race."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        locked = True
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        except OSError:
            locked = False
            log.warning(
                "flock() isn't supported on this filesystem (expected on a "
                "Cloud Storage FUSE mount) — proceeding without it; safe "
                "here because only one worker/instance ever runs at a time."
            )
        try:
            yield
        finally:
            if locked:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _ensure_template_exists(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Consultations"
    ws.append(DEFAULT_HEADERS)
    wb.save(path)
    log.info("No file at %s yet — created a default template. Swap in the real one whenever it's ready.", path)


def _map_columns(header_row: tuple) -> dict[str, int]:
    headers = [(str(cell.value).strip().lower() if cell.value else "") for cell in header_row]
    column_map: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            match = next((i for i, h in enumerate(headers) if alias in h), None)
            if match is not None:
                column_map[field] = match
                break
        else:
            log.warning(
                "Couldn't find a %r column in %s's header row (%s) — that field will be left blank",
                field,
                settings.leads_sheet_path,
                headers,
            )
    return column_map


def append_booking_row(date_str: str, time_str: str, client_name: str, purpose: str) -> None:
    if not settings.leads_sheet_enabled:
        return

    path = Path(settings.leads_sheet_path)

    with _locked(path):
        _ensure_template_exists(path)
        wb = load_workbook(path)
        ws = wb.active

        column_map = _map_columns(ws[1])
        row = [None] * max(ws.max_column, max(column_map.values(), default=-1) + 1)
        values = {"date": date_str, "time": time_str, "client_name": client_name, "purpose": purpose}
        for field, col_idx in column_map.items():
            row[col_idx] = values[field]

        ws.append(row)

        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".xlsx.tmp")
        tmp_path = Path(tmp_name)
        try:
            os.close(fd)
            wb.save(tmp_path)
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    log.info("Appended leads sheet row for %s", client_name)


def format_date_time(iso_str: Optional[str]) -> tuple[str, str]:
    if not iso_str:
        return "", ""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M UTC")
    except ValueError:
        return iso_str, ""


def backfill_missing_rows() -> None:
    """Catches up any booking that never got a row — e.g. everything
    booked before the real template file was dropped in at
    LEADS_SHEET_PATH, or a booking whose webhook-time append failed.
    Safe to re-run; each booking is only appended once
    (leads_sheet_synced_at)."""
    pending = db.get_bookings_needing_leads_sheet_sync()
    if not pending:
        log.info("No bookings pending a leads-sheet row")
        return

    for booking in pending:
        date_str, time_str = format_date_time(booking["start_time"])
        try:
            append_booking_row(
                date_str=date_str,
                time_str=time_str,
                client_name=booking["client_name"] or booking["client_email"],
                purpose=booking["discussion_notes"] or "",
            )
            db.mark_leads_sheet_synced(booking["id"])
        except Exception:
            log.exception("Failed to backfill leads sheet row for booking %s", booking["id"])

    log.info("Backfilled %d leads sheet row(s)", len(pending))


if __name__ == "__main__":
    db.init_db()
    if "--backfill" in sys.argv:
        backfill_missing_rows()
    else:
        print("Usage: python -m app.leads_sheet --backfill")
        print("(Normal operation appends a row per booking automatically from the webhook receiver.)")
