"""Central config loader. Everything the pipeline needs comes from the
environment (see .env.example) so the same code runs unmodified on a
laptop, in tests, or on the production GCP VM."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present; real deployments can also just export the vars.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val not in (None, "") else default
    except ValueError:
        return default


@dataclass  # mutable: tests monkeypatch individual fields (e.g. leads_sheet_path)
class Settings:
    # Calendly
    calendly_signing_secret: str = os.getenv("CALENDLY_SIGNING_SECRET", "")
    calendly_discussion_question: str = os.getenv(
        "CALENDLY_DISCUSSION_QUESTION", "What would you like to discuss?"
    )
    calendly_api_token: str = os.getenv("CALENDLY_API_TOKEN", "")

    # Groq (free-tier LLM API used for summarization — see README for why
    # this isn't Claude/Anthropic despite the plan doc's original wording)
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    # Fireflies.ai — captures each consultation (Requirement 2) and is
    # the transcript source for post-meeting notes + live catch-up.
    # The bot auto-joining meetings is configured in Fireflies' own
    # dashboard (calendar connection), not here.
    fireflies_api_key: str = os.getenv("FIREFLIES_API_KEY", "")
    fireflies_webhook_secret: str = os.getenv("FIREFLIES_WEBHOOK_SECRET", "")

    # Microsoft Graph / Teams
    ms_tenant_id: str = os.getenv("MS_TENANT_ID", "")
    ms_client_id: str = os.getenv("MS_CLIENT_ID", "")
    ms_client_secret: str = os.getenv("MS_CLIENT_SECRET", "")
    # Optional secondary path — plain channel broadcast, never required.
    teams_channel_webhook_url: str = os.getenv("TEAMS_CHANNEL_WEBHOOK_URL", "")
    # Primary path: 1:1 chat with the manager. Default true since every
    # requirement routes through it now; flip to false only if this
    # tenant genuinely doesn't support app-only chat messaging.
    teams_dm_enabled: bool = _bool("TEAMS_DM_ENABLED", True)
    attorney_upn_overrides: dict = field(
        default_factory=lambda: json.loads(os.getenv("ATTORNEY_UPN_OVERRIDES", "{}") or "{}")
    )

    # Scheduling windows
    # Centered on 15 minutes before the call, per Requirement 1.
    brief_lead_min_minutes: int = _int("BRIEF_LEAD_MIN_MINUTES", 13)
    brief_lead_max_minutes: int = _int("BRIEF_LEAD_MAX_MINUTES", 17)

    # Live catch-up (periodic push while a meeting is assumed ongoing)
    catchup_enabled: bool = _bool("CATCHUP_ENABLED", True)
    catchup_max_meeting_minutes: int = _int("CATCHUP_MAX_MEETING_MINUTES", 90)
    catchup_max_pushes: int = _int("CATCHUP_MAX_PUSHES", 6)
    catchup_min_new_chars: int = _int("CATCHUP_MIN_NEW_CHARS", 200)

    # Leads sheet
    leads_sheet_enabled: bool = _bool("LEADS_SHEET_ENABLED", True)
    leads_sheet_path: str = os.getenv("LEADS_SHEET_PATH", "leads.xlsx")

    # Storage / service
    # Firestore project ID. Empty = auto-detect from the environment (this
    # is what happens on Cloud Run, where the attached service account
    # already identifies the project — nothing to set there). Only needed
    # locally if the Firestore client can't otherwise tell which project
    # to talk to.
    gcp_project_id: str = os.getenv("GCP_PROJECT_ID", "")

    webhook_host: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    # Cloud Run injects PORT itself (typically 8080) and expects the
    # container to listen on it; WEBHOOK_PORT stays as the override for
    # local dev where PORT isn't set. PORT wins whenever it's present.
    webhook_port: int = _int("PORT", _int("WEBHOOK_PORT", 8000))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Shared secret for the two internal scheduled-job endpoints
    # (/internal/trigger/brief-scheduler, /internal/trigger/live-catchup).
    # Cloud Run only runs code while handling a request — there's no
    # persistent process for a crontab entry to live in — so Cloud
    # Scheduler calls these URLs on a schedule instead. This secret (sent
    # as the X-Internal-Trigger-Secret header) keeps them from being
    # triggerable by anyone who finds the URL.
    internal_trigger_secret: str = os.getenv("INTERNAL_TRIGGER_SECRET", "")

    def attorney_upn(self, calendly_email: str) -> str:
        """Map a Calendly host/attorney email to their Microsoft 365 UPN.
        Assumes they're identical unless overridden (see ATTORNEY_UPN_OVERRIDES)."""
        return self.attorney_upn_overrides.get(calendly_email, calendly_email)


settings = Settings()
