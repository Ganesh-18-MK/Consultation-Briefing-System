#!/usr/bin/env python3
"""Dev entrypoint. In production run via gunicorn instead:
    gunicorn -w 2 -b 0.0.0.0:8000 app.webhook_receiver:app
(see deploy/calendly-webhook.service)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.webhook_receiver import app
from app.config import settings
from app import db

if __name__ == "__main__":
    db.init_db()
    app.run(host=settings.webhook_host, port=settings.webhook_port, debug=False)
