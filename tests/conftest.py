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
    a test that exercises a code path calling leads_sheet.record_client_response
    (e.g. the Calendly webhook handler) can never write into the real
    leads.xlsx sitting in the project directory. Tests that specifically
    exercise leads_sheet.py already override this explicitly with their
    own tmp_path destination."""
    monkeypatch.setattr(settings, "leads_sheet_path", str(tmp_path / "_autouse_leads.xlsx"))


@pytest.fixture(autouse=True)
def mock_summarizer(request, monkeypatch):
    """The Calendly webhook handler now summarizes the client's answer
    with Groq immediately (for the leads sheet, not just the later Teams
    brief) — autouse-mocked so no test hits the real Groq API just by
    exercising the webhook path. Tests that care about the actual
    summary text (e.g. most brief_scheduler tests, leads-sheet formatting
    tests) override this with their own monkeypatch.setattr call, which
    simply wins over this one within that same test.

    Skipped entirely for tests/test_summarizer.py, whose whole point is
    exercising the real (unmocked) summarize_discussion_notes/_complete
    behavior — autouse-patching it there would defeat those tests."""
    if request.module.__name__.rsplit(".", 1)[-1] == "test_summarizer":
        yield
        return

    from app import summarizer

    monkeypatch.setattr(
        summarizer,
        "summarize_discussion_notes",
        lambda name, notes: "1. Mock summary point one.\n2. Mock summary point two.",
    )
    yield
