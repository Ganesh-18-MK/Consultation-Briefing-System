import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.config import settings
from app import db as db_module
from tests.fake_firestore import FakeFirestoreClient


@pytest.fixture()
def temp_db(monkeypatch):
    """Point the app at a fresh, isolated in-memory fake Firestore for
    the duration of a test — see tests/fake_firestore.py for why this is
    a hand-written fake rather than the real Firestore emulator."""
    fake_client = FakeFirestoreClient()
    monkeypatch.setattr(db_module, "_client_instance", fake_client)
    db_module.init_db()
    yield fake_client


@pytest.fixture()
def no_signature_check(monkeypatch):
    """Calendly signature verification is skipped when the signing secret
    is unset (with a warning) — convenient default for webhook tests that
    aren't specifically testing signature handling."""
    monkeypatch.setattr(settings, "calendly_signing_secret", "")


@pytest.fixture(autouse=True)
def isolated_leads_sheet_path(tmp_path, monkeypatch):
    """Every test gets its own throwaway leads-sheet path by default, so
    a test that exercises a code path calling leads_sheet.append_booking_row
    (e.g. the Calendly webhook handler) can never write into the real
    leads.xlsx sitting in the project directory. Tests that specifically
    exercise leads_sheet.py already override this explicitly with their
    own tmp_path destination."""
    monkeypatch.setattr(settings, "leads_sheet_path", str(tmp_path / "_autouse_leads.xlsx"))
