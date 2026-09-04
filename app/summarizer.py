"""LLM calls used by this pipeline: the pre-call brief's discussion-notes
summary, repeat-client history summaries, post-meeting transcript notes,
and mid-call catch-up recaps — kept separate so any one prompt can be
tuned without touching the others.

Requirement 1's Calendly question ("Please share anything that will help
prepare for our meeting.") can come back as one line or as a long,
detailed narrative (case facts, dates, dollar amounts, deadlines) — see
summarize_discussion_notes below, which condenses it to at most 6-7
sentences without losing the concrete facts an attorney needs, and
without padding out an already-short answer.

Runs on Groq's free API tier (https://console.groq.com) rather than
Anthropic's Claude, which is what the original plan document assumed —
switched to keep this pipeline's running cost at $0 (see README). Groq
serves open-weight models (default here: OpenAI's open-weight GPT-OSS 120B) through an
OpenAI-compatible chat completions API. The trade-offs versus Claude:
- Free tier is rate-limited (requests/day and tokens/day, per Groq
  account — see README for current numbers) rather than pay-as-you-go.
- Output quality/instruction-following on a 70B open model is generally
  a step below a frontier model like Claude on nuanced summarization;
  worth a manual quality check against a handful of real bookings.
Swapping back to Claude (or any other provider) later only means
rewriting `_complete()` below — every calling function's signature
stays the same.
"""
from __future__ import annotations

from groq import Groq

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


def _complete(system: str, user: str, max_tokens: int = 500, reasoning_effort: str = "low") -> str:
    """`reasoning_effort="low"` matters here: the default Groq model
    (a GPT-OSS reasoning model) spends part of max_tokens "thinking"
    before writing the actual answer, separate from the visible content.
    On a tight max_tokens budget that thinking can eat the whole thing,
    coming back with a *successful* API response whose content is just
    an empty string — no error, just nothing. "low" keeps that internal
    budget small for a plain condensing task like this; if summaries
    ever come back empty again, that's the first thing to check
    (a bigger max_tokens, or bumping this to "medium")."""
    response = _get_client().chat.completions.create(
        model=settings.groq_model,
        max_tokens=max_tokens,
        temperature=0.2,  # summarization should stay close to the source, not creative
        reasoning_effort=reasoning_effort,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (response.choices[0].message.content or "").strip()


DISCUSSION_SYSTEM_PROMPT = """You prepare pre-call consultation briefs for attorneys at an \
immigration law firm, based on what a client wrote when answering "Please share anything that \
will help prepare for our meeting" on the booking form. That answer varies enormously — a single \
short sentence, a detailed case narrative with specific facts, or a long list of many distinct \
questions (common for visa clients juggling several filings at once).

Summarize it in at most 6-7 sentences, short enough for an attorney to read in under a minute \
before the call. Adapt to what kind of answer it is:
- A narrative with concrete facts (dates, deadlines, dollar amounts, named people or companies, \
statuses): preserve those facts — do not drop them just to hit a shorter length.
- A long list of many distinct questions, too many to restate individually in 6-7 sentences: do \
NOT try to cram every question in as a run-on sentence. Instead, group them into the 2-4 real \
underlying decisions or themes at stake, and call out by name only the most time-sensitive or \
highest-stakes items (a specific deadline, an irreversible choice). Make it clear this is a \
condensed overview, not the full list — the attorney can read the client's original answer for \
every individual question.
Do not invent or infer anything the client didn't say. If the client's answer is already short, \
do NOT pad it out to reach 6-7 sentences — return it as concisely as it deserves, even a single \
sentence. If no answer was provided, say so plainly in one sentence instead of guessing. Stay \
strictly descriptive: summarize what the client said and asked, but do not add your own advice, \
recommendations, or a suggested order to address things in — prioritizing and advising is the \
attorney's judgment call, not something to include in the summary. Do not include a preamble or \
heading — output only the summary itself."""


def summarize_discussion_notes(client_name: str, raw_notes: str | None) -> str:
    if not raw_notes or not raw_notes.strip():
        return "No answer was provided for this question at booking."
    user = f"Client name: {client_name}\n\nWhat the client wrote when booking:\n{raw_notes}"
    # Generous max_tokens on purpose — some of these (e.g. a demand-letter
    # fact pattern with dates/dollar amounts/multiple parties) are long,
    # and a reasoning model needs real headroom beyond the visible 6-7
    # sentences (see the reasoning_effort note on _complete above).
    return _complete(DISCUSSION_SYSTEM_PROMPT, user, max_tokens=700)


HISTORY_SYSTEM_PROMPT = """You prepare short case-history briefings for attorneys at a law firm \
ahead of a returning client's consultation. You'll be given summarized notes from that client's \
prior meetings, most recent first. Write a short case history (3-6 sentences or bullet points) \
covering what's been discussed before and where things stand. Note if information is dated or \
notes are sparse. Do not invent details. Do not include a preamble or heading."""


def summarize_client_history(client_name: str, prior_notes: list[str]) -> str:
    if not prior_notes:
        return "No prior meeting notes on file."
    numbered = "\n\n".join(f"Meeting {i + 1} (most recent first):\n{note}" for i, note in enumerate(prior_notes))
    user = f"Client name: {client_name}\n\nPrior meeting notes:\n{numbered}"
    return _complete(HISTORY_SYSTEM_PROMPT, user, max_tokens=700)


TRANSCRIPT_SYSTEM_PROMPT = """You turn a raw Teams meeting transcript from a law firm client \
consultation into concise case notes for the file. Summarize: what was discussed, key facts the \
client shared, and any next steps or follow-ups mentioned. Write in plain professional language, \
as bullet points or short paragraphs. Do not include filler like "the meeting began with..." or \
transcribe verbatim dialogue. Do not include a preamble or heading. If the transcript is mostly \
noise or too short to summarize meaningfully, say so plainly instead of padding the output."""


def summarize_transcript(client_name: str, transcript_text: str) -> str:
    user = f"Client name: {client_name}\n\nMeeting transcript:\n{transcript_text}"
    return _complete(TRANSCRIPT_SYSTEM_PROMPT, user, max_tokens=900)


CATCHUP_SYSTEM_PROMPT = """You write short "here's what you missed" recaps for an attorney joining a \
law firm client consultation late. You'll be given a partial transcript of the meeting so far. \
Summarize what has been discussed up to this point in 3-5 bullet points, focused on helping someone \
jump in mid-conversation: what topics have come up, what the client has said, and where the \
conversation currently stands. Write in plain, professional language. Do not invent anything not in \
the transcript, and do not comment on the transcript being partial or incomplete. Do not include a \
preamble or heading — output only the bullet points, each starting with "- "."""


def summarize_live_catchup(client_name: str, transcript_so_far: str) -> str:
    user = f"Client name: {client_name}\n\nMeeting transcript so far (partial — meeting is still in progress):\n{transcript_so_far}"
    return _complete(CATCHUP_SYSTEM_PROMPT, user, max_tokens=600)
