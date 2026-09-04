"""Microsoft Graph app-only auth. Used only for delivering messages to
the manager's Teams chat now (app/teams_delivery.py) — transcript
capture moved to Fireflies (app/fireflies_client.py), so this file no
longer talks to the OnlineMeetings/transcripts endpoints at all.

Requires an Entra ID app registration with, at minimum, application
permissions Chat.Create + ChatMessage.Send, admin-consented (see
README). That's a lighter permission set than the earlier
Graph-transcript approach needed (no OnlineMeetingTranscript.Read.All).
"""
from __future__ import annotations

import time

import msal

from app.config import settings

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_AUTHORITY_TMPL = "https://login.microsoftonline.com/{tenant}"
_SCOPE = ["https://graph.microsoft.com/.default"]

_msal_app: msal.ConfidentialClientApplication | None = None
_token_cache: dict = {"value": None, "expires_at": 0}


def _get_msal_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            client_id=settings.ms_client_id,
            client_credential=settings.ms_client_secret,
            authority=_AUTHORITY_TMPL.format(tenant=settings.ms_tenant_id),
        )
    return _msal_app


def get_token() -> str:
    """App-only access token, cached until shortly before expiry."""
    if _token_cache["value"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["value"]

    result = _get_msal_app().acquire_token_for_client(scopes=_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire Graph token: {result.get('error')}: {result.get('error_description')}"
        )

    _token_cache["value"] = result["access_token"]
    _token_cache["expires_at"] = time.time() + result.get("expires_in", 3600)
    return _token_cache["value"]


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}
