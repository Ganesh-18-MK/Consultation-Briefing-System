"""Verifies Calendly webhook signatures.

Calendly signs each delivery with the header `Calendly-Webhook-Signature`,
shaped like `t=<unix_ts>,v1=<hex_hmac>`, where the hmac is
HMAC-SHA256(signing_secret, f"{t}.{raw_body}"). See:
https://developer.calendly.com/api-docs/webhook-signatures
"""
from __future__ import annotations

import hashlib
import hmac
import time

from app.logging_config import get_logger

log = get_logger(__name__)

DEFAULT_TOLERANCE_SECONDS = 5 * 60


class SignatureError(Exception):
    pass


def _parse_header(header_value: str) -> tuple[str, str]:
    parts = dict(
        item.split("=", 1) for item in header_value.split(",") if "=" in item
    )
    if "t" not in parts or "v1" not in parts:
        raise SignatureError("malformed Calendly-Webhook-Signature header")
    return parts["t"], parts["v1"]


def verify_signature(
    raw_body: bytes,
    signature_header: str | None,
    signing_secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Raises SignatureError if the payload doesn't check out. Callers
    should reject the request (4xx) when this raises."""
    if not signing_secret:
        # Explicit opt-out for local dev only; production must set the secret.
        log.warning("CALENDLY_SIGNING_SECRET not set — skipping signature check")
        return

    if not signature_header:
        raise SignatureError("missing Calendly-Webhook-Signature header")

    timestamp, signature = _parse_header(signature_header)

    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("non-numeric timestamp in signature header")

    if tolerance_seconds and abs(time.time() - ts) > tolerance_seconds:
        raise SignatureError("signature timestamp outside tolerance window")

    signed_payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(signing_secret.encode(), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise SignatureError("signature mismatch")
