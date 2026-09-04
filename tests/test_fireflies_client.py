import hashlib
import hmac

from app import fireflies_client


# ─── normalize_join_url ─────────────────────────────────────────────────

def test_normalize_join_url_strips_query_and_trailing_slash():
    a = fireflies_client.normalize_join_url("https://teams.microsoft.com/l/meetup-join/ABC/?context=xyz")
    b = fireflies_client.normalize_join_url("HTTPS://Teams.Microsoft.com/l/meetup-join/abc")
    assert a == b


def test_normalize_join_url_handles_none_and_empty():
    assert fireflies_client.normalize_join_url(None) == ""
    assert fireflies_client.normalize_join_url("") == ""


def test_normalize_join_url_different_paths_differ():
    a = fireflies_client.normalize_join_url("https://teams.microsoft.com/l/meetup-join/ABC")
    b = fireflies_client.normalize_join_url("https://teams.microsoft.com/l/meetup-join/XYZ")
    assert a != b


# ─── verify_webhook_signature ───────────────────────────────────────────

def test_verify_webhook_signature_accepts_valid_hmac():
    secret = "shh"
    body = b'{"meetingId": "m-1"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert fireflies_client.verify_webhook_signature(body, sig, secret) is True


def test_verify_webhook_signature_accepts_prefixed_header():
    secret = "shh"
    body = b'{"meetingId": "m-1"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert fireflies_client.verify_webhook_signature(body, f"sha256={sig}", secret) is True


def test_verify_webhook_signature_rejects_wrong_signature():
    body = b'{"meetingId": "m-1"}'
    assert fireflies_client.verify_webhook_signature(body, "deadbeef", "shh") is False


def test_verify_webhook_signature_rejects_missing_header():
    body = b'{"meetingId": "m-1"}'
    assert fireflies_client.verify_webhook_signature(body, None, "shh") is False


def test_verify_webhook_signature_skips_check_when_secret_unset():
    body = b'{"meetingId": "m-1"}'
    assert fireflies_client.verify_webhook_signature(body, None, "") is True
    assert fireflies_client.verify_webhook_signature(body, "anything", "") is True


# ─── transcript_text ────────────────────────────────────────────────────

def test_transcript_text_formats_speaker_lines():
    transcript = {
        "sentences": [
            {"speaker_name": "Jane Client", "text": "I need help with a visa."},
            {"speaker_name": "Attorney", "text": "Sure, let's go through it."},
        ]
    }
    text = fireflies_client.transcript_text(transcript)
    assert text == "Jane Client: I need help with a visa.\nAttorney: Sure, let's go through it."


def test_transcript_text_skips_empty_lines_and_missing_speaker():
    transcript = {
        "sentences": [
            {"speaker_name": None, "text": "unattributed"},
            {"speaker_name": "Attorney", "text": "   "},
        ]
    }
    text = fireflies_client.transcript_text(transcript)
    assert text == "Speaker: unattributed"


def test_transcript_text_handles_no_sentences():
    assert fireflies_client.transcript_text({}) == ""
    assert fireflies_client.transcript_text({"sentences": None}) == ""


# ─── find_active_meeting_id / get_live_transcript_text (mocked _query) ──

def test_find_active_meeting_id_matches_normalized_link(monkeypatch):
    def fake_query(query, variables):
        return {
            "active_meetings": [
                {"id": "m-1", "meeting_link": "https://teams.microsoft.com/l/meetup-join/ABC/?context=xyz"},
                {"id": "m-2", "meeting_link": "https://teams.microsoft.com/l/meetup-join/OTHER"},
            ]
        }

    monkeypatch.setattr(fireflies_client, "_query", fake_query)
    meeting_id = fireflies_client.find_active_meeting_id(
        "attorney@example.com", "https://teams.microsoft.com/l/meetup-join/abc"
    )
    assert meeting_id == "m-1"


def test_find_active_meeting_id_returns_none_when_no_match(monkeypatch):
    monkeypatch.setattr(fireflies_client, "_query", lambda query, variables: {"active_meetings": []})
    meeting_id = fireflies_client.find_active_meeting_id(
        "attorney@example.com", "https://teams.microsoft.com/l/meetup-join/abc"
    )
    assert meeting_id is None


def test_find_active_meeting_id_returns_none_for_empty_join_url(monkeypatch):
    called = []
    monkeypatch.setattr(fireflies_client, "_query", lambda query, variables: called.append(1))
    assert fireflies_client.find_active_meeting_id("attorney@example.com", "") is None
    assert called == []


def test_get_live_transcript_text_end_to_end(monkeypatch):
    def fake_query(query, variables):
        if "active_meetings" in query:
            return {"active_meetings": [{"id": "m-1", "meeting_link": "https://teams.microsoft.com/l/meetup-join/abc"}]}
        return {"transcript": {"sentences": [{"speaker_name": "Jane Client", "text": "hello"}]}}

    monkeypatch.setattr(fireflies_client, "_query", fake_query)
    text = fireflies_client.get_live_transcript_text(
        "attorney@example.com", "https://teams.microsoft.com/l/meetup-join/abc"
    )
    assert text == "Jane Client: hello"


def test_get_live_transcript_text_returns_none_when_no_active_meeting(monkeypatch):
    monkeypatch.setattr(fireflies_client, "_query", lambda query, variables: {"active_meetings": []})
    text = fireflies_client.get_live_transcript_text(
        "attorney@example.com", "https://teams.microsoft.com/l/meetup-join/abc"
    )
    assert text is None
