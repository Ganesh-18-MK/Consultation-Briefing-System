from app import summarizer


def test_summarize_discussion_notes_short_circuits_on_no_answer(monkeypatch):
    """No Calendly answer at all — should return a fixed message without
    ever calling out to Groq (no point spending an API call on nothing)."""
    called = []
    monkeypatch.setattr(summarizer, "_complete", lambda *a, **k: called.append(1))

    assert summarizer.summarize_discussion_notes("Jane Client", None) == "No answer was provided for this question at booking."
    assert summarizer.summarize_discussion_notes("Jane Client", "   ") == "No answer was provided for this question at booking."
    assert called == []


def test_summarize_discussion_notes_delegates_to_complete(monkeypatch):
    captured = {}

    def fake_complete(system, user, max_tokens=500, reasoning_effort="low"):
        captured["system"] = system
        captured["user"] = user
        captured["max_tokens"] = max_tokens
        return "condensed summary"

    monkeypatch.setattr(summarizer, "_complete", fake_complete)

    result = summarizer.summarize_discussion_notes("Jane Client", "Some raw notes about a visa question.")

    assert result == "condensed summary"
    assert "Jane Client" in captured["user"]
    assert "Some raw notes about a visa question." in captured["user"]
    # Generous headroom for a reasoning model — see _complete's docstring
    # for why a tight budget can come back silently empty.
    assert captured["max_tokens"] >= 700


def test_complete_defaults_to_low_reasoning_effort(monkeypatch):
    """Regression test for the empty-summary bug: the default Groq model
    is a reasoning model that can burn the whole token budget "thinking"
    before writing anything, so every call through _complete should ask
    for low reasoning effort unless a caller explicitly overrides it."""
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Choice:
                class message:
                    content = "the answer"

            class Response:
                choices = [Choice()]

            return Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(summarizer, "_get_client", lambda: FakeClient())

    result = summarizer._complete("system prompt", "user prompt")

    assert result == "the answer"
    assert captured["reasoning_effort"] == "low"
    assert captured["max_tokens"] == 500  # the _complete default


def test_complete_returns_empty_string_not_none_when_content_missing(monkeypatch):
    """If a call ever does come back with no visible content (e.g. a too-
    tight max_tokens budget consumed entirely by reasoning), callers
    should get "" rather than a None that would blow up string concat
    elsewhere (brief_scheduler's brief_summary, for one)."""
    class FakeCompletions:
        def create(self, **kwargs):
            class Choice:
                class message:
                    content = None

            class Response:
                choices = [Choice()]

            return Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(summarizer, "_get_client", lambda: FakeClient())

    assert summarizer._complete("system", "user") == ""
