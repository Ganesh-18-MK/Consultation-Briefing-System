# Cloud Run image for the webhook receiver (app/webhook_receiver.py).
# Handles /webhooks/calendly, /webhooks/fireflies, and the two internal
# scheduled-job triggers Cloud Scheduler calls in place of cron — see
# README's "Deploying to Cloud Run" section for the full walkthrough.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/init_db.py ./scripts/init_db.py

# Cloud Run sets $PORT (usually 8080) and expects the container to listen
# on it; app/config.py's webhook_port already reads $PORT when present.
ENV PORT=8080
EXPOSE 8080

# 1 worker: this app is low-volume (a handful of bookings/day plus two
# scheduled checks), and Firestore itself handles concurrent writes
# safely, so extra workers wouldn't buy correctness — just cost. --timeout
# raised because a Groq summarization call or a Teams Graph API call can
# occasionally take a few seconds longer than gunicorn's 30s default,
# especially on a cold start.
CMD exec gunicorn -w 1 --timeout 60 -b 0.0.0.0:${PORT} app.webhook_receiver:app
