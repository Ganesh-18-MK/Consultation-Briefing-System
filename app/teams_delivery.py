"""Delivering messages into the manager's Teams chat — the primary path
for all three notification types the manager asked for: the pre-call
brief (Requirement 1), the mid-call catch-up (Requirement 3), and the
post-meeting notes (Requirement 4). Since there's one manager attending
these consultations, everything lands in one ongoing private chat with
her rather than a broadcast channel.

Mechanics: a Power Automate "Workflows" incoming webhook, pointed at
the manager's own private Teams chat (Teams > search "Workflows" >
template "Send webhook alerts to a chat" > connect her account > pick
her own chat as the destination > copy the generated HTTP POST URL into
TEAMS_MANAGER_WEBHOOK_URL). We POST an Adaptive Card to that URL, same
shape as the optional channel broadcast below.

This replaces an earlier Graph app-only Chat.Create + ChatMessage.Send
design, which turned out to be a dead end: Microsoft only exposes
ChatMessage.Send as a **Delegated** permission (requires an actual
signed-in person clicking through consent), never as an **Application**
permission — so there is no way to send a 1:1 Teams chat message via
client-credentials (app-only) auth, no matter what's granted in the
Azure admin center. The Workflows-webhook approach sidesteps that
entirely: no Graph permissions, no admin consent, nothing to break.

`TEAMS_DM_ENABLED` (default true) is the escape hatch to silence
manager-chat delivery without unsetting the webhook URL. Every caller
degrades gracefully (logs and returns False) rather than crashing a
scheduler run if the webhook isn't configured or the POST fails.

An optional secondary path — a plain channel broadcast, also via a
Workflows incoming webhook — still exists below (`post_channel_brief`)
for anyone who also wants a team-visible copy. It's off unless
TEAMS_CHANNEL_WEBHOOK_URL is set and is never required.
"""
from __future__ import annotations

import requests

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _adaptive_card(title: str, facts: list[tuple[str, str]], body_text: str) -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    if facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]})
    body.append({"type": "TextBlock", "text": body_text, "wrap": True})
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }


def send_manager_message(manager_upn: str, title: str, lines: list[str]) -> bool:
    """Posts one message into the manager's private Teams chat via the
    Workflows webhook. `lines` are joined under a bold title — used for
    the brief, the catch-up push, and the post-meeting notes alike.
    `manager_upn` is accepted for logging/parity with callers but no
    longer used to address the message — the webhook URL itself already
    points at one fixed chat."""
    if not settings.teams_dm_enabled:
        log.info("TEAMS_DM_ENABLED=false — skipping manager chat message %r", title)
        return False

    if not settings.teams_manager_webhook_url:
        log.error(
            "TEAMS_MANAGER_WEBHOOK_URL is not set — cannot deliver manager chat message %r "
            "(see README for the Power Automate Workflows setup)",
            title,
        )
        return False

    body_text = "\n\n".join(line for line in lines if line)
    card = _adaptive_card(title, [], body_text)

    try:
        resp = requests.post(settings.teams_manager_webhook_url, json=card, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Failed to send Teams chat message %r to %s: %s", title, manager_upn, exc)
        return False


# ─── Optional secondary path: plain channel broadcast ──────────────────

def post_channel_brief(
    client_name: str,
    attorney_email: str,
    start_time_iso: str,
    discussion_summary: str,
    case_history: str | None,
) -> bool:
    """Optional team-visible copy of the brief, via the channel Workflows
    webhook. Never required — the manager's private chat (above) is the
    path that actually satisfies the requirement. Only runs if
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
