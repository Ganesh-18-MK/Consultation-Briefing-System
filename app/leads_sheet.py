"""Requirement 5: a shared-drive Excel file with one row PER CLIENT
(not per booking), keyed by email. Columns A/B are fixed (Client Name,
Email); every booking after that adds — or reuses — a column headed by
that booking's date, and the cell at [that client's row, that date
column] gets the questions the client answered on Calendly for that
specific booking.

So a first-time client gets a new row plus (if no other client has
ever booked that exact date) a new date column. A repeat client on a
new date reuses their existing row and adds/reuses a column for the
new date — their older date columns are left untouched, not overwritten.
Two different clients who happen to book the same date share that one
column (each gets their own answer in their own row under it).

This file is owned entirely by this code (unlike the old row-per-booking
version, which matched an arbitrary pre-existing manager template) —
column layout is fixed by us, not discovered from an existing header
row. If nothing exists yet at LEADS_SHEET_PATH, a fresh file with just
the two fixed headers is created automatically.
"""
from __future__ import annotations

import fcntl
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment

from app import db, summarizer
from app.timezones import to_central
from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

CLIENT_NAME_COL = 1  # column A
EMAIL_COL = 2        # column B
OWNER_COL = 3        # column C — blank on creation; mam/staff fill this in
                      # by hand in Excel/OneDrive to assign a client to a
                      # staff member. The app never reads or writes it
                      # after the row is first created.
FIRST_DATE_COL = 4   # column D onward — one per distinct booking date

FIXED_HEADERS = ["Client Name", "Email", "Owner"]


@contextmanager
def _locked(path: Path):
    """A simple cross-process advisory lock so two near-simultaneous
    bookings can't read-modify-write the same file at once and stomp on
    each other's row/column. On a real filesystem (the VM path, or
    local dev) this is a hard guarantee via flock(). On Cloud Run this
    file instead lives on a mounted Cloud Storage bucket (see README's
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
    ws.append(FIXED_HEADERS)
    wb.save(path)
    log.info("No file at %s yet — created a fresh one with the client/email headers.", path)


def _find_or_create_date_column(ws, date_str: str) -> int:
    """Returns the 1-based column index for this date's header, creating
    it at the end of the header row if it doesn't already exist. A newly
    created column gets a wider default width so the wrapped numbered
    list underneath (see record_client_response) is actually legible
    rather than a narrow sliver."""
    header_row = ws[1]
    for cell in header_row:
        if cell.column < FIRST_DATE_COL:
            continue
        if cell.value is not None and str(cell.value).strip() == date_str:
            return cell.column

    new_col = max(ws.max_column, FIRST_DATE_COL - 1) + 1
    ws.cell(row=1, column=new_col, value=date_str)
    ws.column_dimensions[ws.cell(row=1, column=new_col).column_letter].width = 100
    return new_col


def _find_or_create_client_row(ws, client_name: str, client_email: str) -> int:
    """Returns the 1-based row index for this client (matched by email,
    case-insensitively), creating a new row with Client Name/Email
    filled in if this is their first-ever appearance in the sheet."""
    email_lower = (client_email or "").strip().lower()
    for row_idx in range(2, ws.max_row + 1):
        existing_email = ws.cell(row=row_idx, column=EMAIL_COL).value
        if existing_email and str(existing_email).strip().lower() == email_lower:
            return row_idx

    new_row = ws.max_row + 1 if ws.max_row >= 1 else 2
    if new_row < 2:
        new_row = 2
    ws.cell(row=new_row, column=CLIENT_NAME_COL, value=client_name)
    ws.cell(row=new_row, column=EMAIL_COL, value=client_email)
    return new_row


def record_client_response(client_name: str, client_email: str, date_str: str, questions: str) -> None:
    """Requirement 5, client-rows/date-columns layout: find (or create)
    this client's row and this booking date's column, then write their
    answered questions into that cell. Safe to call repeatedly for the
    same booking (it'll just overwrite the same cell with the same
    value)."""
    if not settings.leads_sheet_enabled:
        return

    path = Path(settings.leads_sheet_path)

    with _locked(path):
        _ensure_template_exists(path)
        wb = load_workbook(path)
        ws = wb.active

        col_idx = _find_or_create_date_column(ws, date_str)
        row_idx = _find_or_create_client_row(ws, client_name, client_email)
        cell = ws.cell(row=row_idx, column=col_idx, value=questions)
        # Without this, a numbered list with embedded newlines ("1. ...\n2.
        # ...") still stores the line breaks correctly, but Excel shows it
        # all run together on one visual line unless the cell has "Wrap
        # Text" turned on — this is what actually makes it render as a
        # vertical numbered list instead of "1. ...2. ...3. ...".
        cell.alignment = Alignment(wrap_text=True, vertical="top")

        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".xlsx.tmp")
        tmp_path = Path(tmp_name)
        try:
            os.close(fd)
            wb.save(tmp_path)
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    log.info("Recorded leads sheet entry for %s (%s) on %s", client_name, client_email, date_str)


def format_date_time(iso_str: Optional[str]) -> tuple[str, str]:
    """Shared by brief_scheduler.py for the Date/Time lines in the Teams
    message text — this module only needs the date half (see
    format_date below) for its own column headers, but keeps this
    around since it's the one place ISO-string parsing for display
    already lived. Displays in US Central Time (mam's actual working
    hours — see app/timezones.py); the date can roll to the next (or
    previous) calendar day relative to the stored UTC value, which is
    intentional — both halves are derived from the same Central-Time
    datetime so the Date and Time lines always agree with each other.
    %Z prints "CST" or "CDT" as actually in effect that day, rather
    than a label that would be wrong for half the year."""
    central_dt = to_central(iso_str)
    if central_dt is None:
        return (iso_str or "", "")
    return central_dt.strftime("%Y-%m-%d"), central_dt.strftime("%I:%M %p %Z")


def format_date(iso_str: Optional[str]) -> str:
    date_str, _ = format_date_time(iso_str)
    return date_str


def backfill_missing_rows() -> None:
    """Catches up any booking that never got recorded — e.g. everything
    booked before this file existed at LEADS_SHEET_PATH, or a booking
    whose webhook-time write failed. Safe to re-run; each booking is
    only processed once (leads_sheet_synced_at)."""
    pending = db.get_bookings_needing_leads_sheet_sync()
    if not pending:
        log.info("No bookings pending a leads-sheet entry")
        return

    for booking in pending:
        date_str = format_date(booking["start_time"])
        client_name = booking["client_name"] or booking["client_email"]
        summary = summarizer.summarize_discussion_notes(client_name, booking["discussion_notes"])
        try:
            record_client_response(
                client_name=client_name,
                client_email=booking["client_email"] or "",
                date_str=date_str,
                questions=summary,
            )
            db.mark_leads_sheet_synced(booking["id"])
        except Exception:
            log.exception("Failed to backfill leads sheet entry for booking %s", booking["id"])

    log.info("Backfilled %d leads sheet entr(y/ies)", len(pending))


if __name__ == "__main__":
    db.init_db()
    if "--backfill" in sys.argv:
        backfill_missing_rows()
    else:
        print("Usage: python -m app.leads_sheet --backfill")
        print("(Normal operation records an entry per booking automatically from the webhook receiver.)")
