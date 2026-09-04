"""Delivering messages into the manager's Teams chat — the primary path
for all three notification types the manager asked for: the pre-call
brief (Requirement 1), the mid-call catch-up (Requirement 3), and the
post-meeting notes (Requirement 4). Since there's one manager attending
these consultations, everything lands in one ongoing 1:1 chat with them
rather than a broadcast channel.

Mechanics: Microsoft Graph app-only auth (app/graph_client.py) +
POST /chats (find-or-create a 1:1 chat) + POST /chats/{id}/messages.
Graph's oneOnOne chat creation is idempotent, so repeated calls land in
the same ongoing thread rather than spawning new chats each time; we
still cache the resolved chat id per process to skip the extra create
call.

**Requires tenant admin consent** for Chat.Create + ChatMessage.Send
application permissions (see README) — this is a one-time IT setup
step, not a running cost, but not every tenant allows app-only chat
messaging by default. `TEAMS_DM_ENABLED` (default true) is the escape
hatch if it turns out this tenant doesn't support it; every caller
degrades gracefully (logs and returns False) rather than crashing a
scheduler run.

An optional secondary path — a plain channel broadcast via Incoming
Webhook — still exists below (`post_channel_brief`) for anyone who also
wants a team-visible copy. It's off unless `TEAMS_CHANNEL_WEBHOOK_URL`
is set and is never required.
"""
from __future__ import annotations

import html as html_lib

import requests

from app.config import settings
from app.graph_client import auth_headers as graph_headers
from app.graph_client import GRAPH_BASE
from app.logging_config import get_logger

log = get_logger(__name__)

_manager_chat_id_cache: dict[str, str] = {}


def _get_or_create_manager_chat(manager_upn: str) -> str | None:
    if manager_upn in _manager_chat_id_cache:
        return _manager_chat_id_cache[manager_upn]

    try:
        resp = requests.post(
            f"{GRAPH_BASE}/chats",
            headers=graph_headers(),
            json={
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{manager_upn}')",
                    }
                ],
            },
            timeout=15,
        )
        resp.raise_for_status()
        chat_id = resp.json()["id"]
        _manager_chat_id_cache[manager_upn] = chat_id
        return chat_id
    except requests.RequestException as exc:
        log.error(
            "Could not open/find a Teams chat with %s (needs tenant admin consent for "
            "Chat.Create — see README): %s",
            manager_upn,
            exc,
        )
        return None


def send_manager_message(manager_upn: str, title: str, lines: list[str]) -> bool:
    """Posts one message into the manager's private Teams chat. `lines`
    are joined with line breaks under a bold title — used for the brief,
    the catch-up push, and the post-meeting notes alike."""
    if not settings.teams_dm_enabled:
        log.info("TEAMS_DM_ENABLED=false — skipping manager chat message %r", title)
        return False

    chat_id = _get_or_create_manager_chat(manager_upn)
    if not chat_id:
        return False

    body_html = f"<strong>{html_lib.escape(title)}</strong><br><br>" + "<br>".join(
        html_lib.escape(line).replace("\n", "<br>") for line in lines if line
    )

    try:
        resp = requests.post(
            f"{GRAPH_BASE}/chats/{chat_id}/messages",
            headers=graph_headers(),
            json={"body": {"contentType": "html", "content": body_html}},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Failed to send Teams chat message %r to %s: %s", title, manager_upn, exc)
        return False


# ─── Optional secondary path: plain channel broadcast ──────────────────

def _adaptive_card(title: str, facts: list[tuple[str, str]], body_text: str) -> dict:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
                        {
                            "type": "FactSet",
                            "facts": [{"title": k, "value": v} for k, v in facts],
                        },
                        {"type": "TextBlock", "text": body_text, "wrap": True},
                    ],
                },
            }
        ],
    }


def post_channel_brief(
    client_name: str,
    attorney_email: str,
    start_time_iso: str,
    discussion_summary: str,
    case_history: str | None,
) -> bool:
    """Optional team-visible copy of the brief, via Incoming Webhook.
    Never required — the manager's private chat (above) is the path
    that actually satisfies the requirement. Only runs if
    TEAMS_CHANNEL_WEBHOOK_URL is set."""
    if not settings.teams_channel_webhook_url:
        return False

    title = f"Upcoming consultation: {client_name}"
    facts = [("Manager", attorney_email), ("Starts", start_time_iso)]
    body_lines = [discussion_summary]
    if case_history:
        body_lines.append("\n**Prior history (repeat client):**\n" + case_history)
    body_text = "\n\n".join(body_lines)

    card = _adaptive_card(title, facts, body_text)

    try:
        resp = requests.post(settings.teams_channel_webhook_url, json=card, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Failed to post optional channel copy of brief for %s: %s", client_name, exc)
        return False
