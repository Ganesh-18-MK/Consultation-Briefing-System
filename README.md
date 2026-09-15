# Consultation Pre-Call Briefing System

Automates five things around every Calendly consultation, end to end,
hosted on Google Cloud Run — at no ongoing cost beyond pennies of AI API
usage, comfortably inside Cloud Run/Firestore's free tiers at this
volume (see "Deploying to Cloud Run" below):

1. **Pre-call brief.** About 15 minutes before each consultation starts,
   the manager gets a Microsoft Teams message (in their own private 1:1
   chat with the bot) with the client's name, the meeting's date/time,
   and what the client answered on the Calendly booking form ("Please
   share anything that will help prepare for our meeting.") — that
   answer can be one line or a long case narrative, so it's condensed
   with Groq to at most 6-7 sentences, keeping concrete facts (names,
   dates, dollar amounts, deadlines) rather than dropping them for
   brevity, and left alone rather than padded out if it's already
   short. For a repeat client, a Groq-generated summary of their prior
   consultation history is added underneath.
2. **Live capture.** Once the meeting starts, Fireflies.ai (a meeting
   bot that auto-joins from the manager's calendar) records and
   transcribes it in parallel — no one has to start anything manually.
3. **Mid-call catch-up.** While the meeting is still running, a summary
   of what's been discussed so far gets pushed into that same private
   manager chat every few minutes, so anyone joining late (or the
   manager themself) can catch up without asking.
4. **Post-meeting notes.** Once Fireflies finishes processing the
   transcript, an AI-generated summary — client name, date, consultation
   purpose, and what was actually discussed — lands in the manager's
   Teams chat automatically.
5. **Leads spreadsheet.** Every booking adds one row (Date, Time, Client
   Name, Purpose) to a shared Excel file the moment it's made, so intake
   staff always have a running list.

This is a from-scratch rebuild of the system described in
`Consultation_Briefing_System_Plan.docx`, redesigned around a specific
requirement: **the manager is the one attending consultations**, so
everything routes to their private Teams chat rather than a shared
channel — see "Why a private chat, not the shared meeting chat" below
for the reasoning on requirement 3 specifically.

## How the pieces map to the requirements

| Requirement | Code |
|---|---|
| 1 — Pre-call brief to manager's Teams chat | `app/brief_scheduler.py`, `app/summarizer.py`, `app/teams_delivery.py` |
| 2 — Fireflies captures the meeting | Set up in Fireflies' own dashboard (see Setup step 2) — nothing to run here |
| 3 — Mid-call catch-up to manager's chat | `app/live_catchup.py`, `app/fireflies_client.py` |
| 4 — Post-meeting notes to manager's chat | `app/webhook_receiver.py` (`/webhooks/fireflies`), `app/fireflies_client.py` |
| 5 — Row per booking in the leads sheet | `app/leads_sheet.py`, called from `app/webhook_receiver.py` (`/webhooks/calendly`) |
| Booking capture / repeat-client detection | `app/webhook_receiver.py`, `app/calendly_client.py`, `app/calendly_signature.py`, `app/db.py` |
| Teams message delivery (all of the above) | `app/teams_delivery.py`, `app/graph_client.py` |

All of this runs as one Cloud Run service (`app/webhook_receiver.py`,
a Flask app behind gunicorn):

- `POST /webhooks/calendly` — booking capture + the leads-sheet row
- `POST /webhooks/fireflies` — fires when a transcript finishes
  (post-meeting notes)
- `POST /internal/trigger/brief-scheduler` and
  `POST /internal/trigger/live-catchup` — Cloud Run only runs code
  while handling a request, so there's no persistent process for a
  crontab entry to live in the way there would be on a VM. Cloud
  Scheduler calls these two URLs instead — every 2 minutes for the
  brief scheduler (looks for consultations starting in ~15 minutes) and
  every 5 minutes for the live catch-up (checks in-progress meetings
  for new content) — and they run the exact same `app/brief_scheduler.py`
  / `app/live_catchup.py` code a cron job would have called directly.
  Protected by a shared secret (`INTERNAL_TRIGGER_SECRET`) so the URLs
  aren't triggerable by anyone who finds them.

The first three are event-driven, not polled; only the last two run on
a schedule.

There's no `notes_sync.py` or scheduled leads-sheet rebuild in this
version — an earlier draft polled Microsoft Graph for transcripts and
regenerated the whole spreadsheet on a timer. Both are now event-driven
(the Fireflies webhook, and one `append` per booking), which is simpler
and faster.

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in `.env` — see the comments in `.env.example` for where each value
comes from. The short version, grouped by what each piece is for:

| What | `.env` variable(s) | Where to get it |
|---|---|---|
| Calendly webhook | `CALENDLY_SIGNING_SECRET` | Calendly > Integrations > Webhooks, when creating the subscription (needs Calendly Standard+) |
| Discussion-topic question | `CALENDLY_DISCUSSION_QUESTION` | Must match the booking form's question text exactly |
| Enriching the booking (start time, manager, Teams link) | `CALENDLY_API_TOKEN` | Calendly > Integrations > API & Webhooks — the webhook payload alone doesn't carry these fields |
| Summarization (free) | `GROQ_API_KEY`, `GROQ_MODEL` | https://console.groq.com — no credit card; see "Why Groq" below |
| Meeting capture | `FIREFLIES_API_KEY`, `FIREFLIES_WEBHOOK_SECRET` | https://app.fireflies.ai — see below, this one needs an extra manual step |
| Delivering Teams messages | `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` | Entra ID app registration, admin-consented (see below) |

**Fireflies setup has a manual step beyond the API key**: in the
Fireflies dashboard, connect the manager's Microsoft 365 calendar and
turn on Auto Join, so the Fireflies bot joins every Teams meeting on
their calendar by itself. That's configured in Fireflies' own settings,
not in this codebase — the API key and webhook secret here only let this
system *read* what Fireflies captured and get notified when a transcript
is ready. Also register this app's `/webhooks/fireflies` URL under
Fireflies > Developer Settings to get the webhook secret.

**Entra ID app registration (Microsoft Graph)**: needs Application
permissions `Chat.Create` + `ChatMessage.Send`, admin-consented by the
tenant. This is the one piece of setup that needs your IT admin/M365
admin — a few tenants restrict app-only chat messaging even with these
permissions granted, so it's worth a quick test send (see step 4) before
trusting it in production. If it turns out not to work in this tenant,
that's a tenant policy question for IT, not a bug in this code — flip
`TEAMS_DM_ENABLED=false` in the meantime and messages simply won't send
(logged, not crashed) until it's sorted out.

**Why Groq instead of the Claude/Anthropic API the original plan
assumed:** this needs to run at $0. Groq (https://console.groq.com — a
different company from xAI's "Grok," easy to mix up) serves open-weight
models (default here: OpenAI's open-weight GPT-OSS 120B) through a
genuinely free API tier: no credit card, just a daily request/token cap
per account (check current numbers on your account's dashboard — Groq
retires and swaps out free models periodically, as happened with the
model this project originally shipped with, so it's worth a glance at
https://console.groq.com/docs/deprecations if summarization ever starts
erroring with a "model not found"). `app/summarizer.py`
is the only file that talks to it; every other module just calls its
functions, so switching providers later is a one-file change. Two things
worth knowing:
- **Quality is a step below Claude** on nuanced summarization, especially
  legal-domain wording. Worth reading through the first real week of
  generated briefs/notes rather than assuming they're as sharp as
  Claude's would be.
- **The free tier can throttle you (HTTP 429)** if booking volume spikes.
  Every job that calls Groq already catches and logs per-booking
  failures without crashing, so a throttled run just means that one
  brief/note gets skipped and retried, not that the service goes down —
  worth watching the logs early on.

**Everything else in this pipeline is genuinely free too**: Fireflies'
free plan, Microsoft Graph (already-licensed Teams/M365, no extra cost
to send app-only chat messages), and the Excel file (no SharePoint or
Google Sheets subscription needed — see the leads sheet section). The
only paid thing anywhere in this system is whatever Calendly plan tier
the firm already has for webhooks (Standard+), which isn't specific to
this build.

### 3. Check the database connection

```bash
python scripts/init_db.py
```

Bookings/clients/notes live in Firestore (see `app/db.py`), not a local
file — Cloud Run's own disk doesn't survive a restart, so a local
SQLite/Excel file would silently lose data. This script is a quick
sanity check that the app can reach your Firestore project; the actual
one-time Firestore setup (enabling it, picking a region, the two
composite indexes) happens once, in "Deploying to Cloud Run" below, not
here.

### 4. Run locally / smoke-test

```bash
python scripts/run_webhook_server.py      # webhook receiver, http://localhost:8000
python -m app.brief_scheduler               # one-off run of the brief job
python -m app.live_catchup                  # one-off run of the catch-up job
```

`GET /healthz` is a liveness check. Point a Calendly webhook subscription
at `POST /webhooks/calendly` and a Fireflies webhook at
`POST /webhooks/fireflies` (use `ngrok` or similar for local testing).

Worth doing once before go-live: book a real test consultation end to
end and confirm a message actually lands in the manager's Teams chat —
that's the step most likely to be blocked by a tenant policy (see the
Entra ID note above), and it's much cheaper to find out now than after
the firm is relying on it.

### 5. Run tests

```bash
pip install -r requirements.txt   # pulls in pytest
pytest
```

Tests use a small hand-written in-memory fake in place of Firestore
(`tests/fake_firestore.py`) plus a throwaway Excel path per test, and
stub out every network call (Groq, Teams/Graph, Calendly, Fireflies) —
no live credentials, and no real GCP project, needed to run the suite.

## Deploying to Cloud Run

One-time setup, then a couple of commands whenever you deploy or
redeploy. All commands assume the `gcloud` CLI is installed and you've
run `gcloud auth login` and `gcloud config set project YOUR_PROJECT_ID`.
Pick one region and use it everywhere below — `us-central1` is used
here since it's one of the three regions eligible for Google's Always
Free allowances (the other two are `us-west1` and `us-east1`); Cloud
Run, Firestore, and the Cloud Storage bucket should all be in the same
region to avoid cross-region latency and egress charges.

### 1. One-time GCP setup

```bash
# Enable the APIs this needs.
gcloud services enable run.googleapis.com firestore.googleapis.com \
    cloudscheduler.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com

# Create the Firestore database — Native mode, the default (unnamed)
# database, in your chosen region. This is a one-time, mostly
# irreversible choice (the region can't be changed later without
# deleting and recreating the database), and only the *default*
# database qualifies for Firestore's free daily quota — a named
# database doesn't.
gcloud firestore databases create --location=us-central1

# Create the two composite indexes app/db.py's queries need (see the
# comments at the top of that file for why only these two, out of
# every query, need one). Takes a few minutes to finish building in the
# background — safe to move on to the next step while it does.
gcloud firestore indexes composite create \
    --collection-group=bookings \
    --field-config field-path=status,order=ascending \
    --field-config field-path=start_time,order=ascending

gcloud firestore indexes composite create \
    --collection-group=bookings \
    --field-config field-path=attorney_email,order=ascending \
    --field-config field-path=notes_synced_at,order=ascending

# Requirement (2026-09-15): mam's two fixed daily consultation blocks —
# see app/timezones.py and app/db.py's get_bookings_due_for_block_brief.
gcloud firestore indexes composite create \
    --collection-group=bookings \
    --field-config field-path=status,order=ascending \
    --field-config field-path=brief_deadline,order=ascending

# Create the bucket the leads spreadsheet lives on — Cloud Run's own
# disk doesn't survive a restart, so this file needs to live somewhere
# that does (see app/leads_sheet.py). A plain SQLite-style file
# wouldn't be safe here, but a single Excel file that's always fully
# rewritten and only ever touched by one worker at a time (see the
# Dockerfile) is.
gcloud storage buckets create gs://YOUR_PROJECT_ID-leads-sheet \
    --location=us-central1 --uniform-bucket-level-access
```

### 2. Deploy the service

```bash
# Generate a random secret Cloud Scheduler will use to call the two
# internal trigger endpoints — save it, you'll need it again in step 3.
openssl rand -hex 32

gcloud run deploy calendly-briefing-system \
    --source . \
    --region=us-central1 \
    --allow-unauthenticated \
    --max-instances=1 \
    --execution-environment=gen2 \
    --add-volume=name=leads-sheet,type=cloud-storage,bucket=YOUR_PROJECT_ID-leads-sheet \
    --add-volume-mount=volume=leads-sheet,mount-path=/data \
    --env-vars-file=.env.deploy.yaml
```

(There's no separate `--set-env-vars` flag here — `gcloud run deploy` treats
`--set-env-vars` and `--env-vars-file` as alternative ways of doing the same
thing, so they can't both appear in one command. The `LEADS_SHEET_PATH`
override for the mount path is folded into the generated YAML instead — see
below.)

A few things about that command:

- `--source .` builds the image from the `Dockerfile` in this project
  and deploys it in one step (via Cloud Build) — no separate build/push
  needed.
- `--allow-unauthenticated` is required: Calendly and Fireflies need to
  be able to reach `/webhooks/calendly` and `/webhooks/fireflies`
  without a Google-issued auth token. The two internal trigger routes
  stay protected by their own shared secret instead (see below) — that,
  not IAM, is what keeps them from being called by anyone else who
  finds the URL.
- `--max-instances=1` matters for the leads spreadsheet: Cloud Run
  could otherwise start a second instance to handle a burst of
  requests, and two instances writing the same mounted file at once is
  the one scenario the single-writer assumption in `app/leads_sheet.py`
  doesn't cover. At this app's actual traffic (a handful of bookings a
  day) one instance is never a bottleneck.
- `--env-vars-file=.env.deploy.yaml` — everything from `.env` (Groq,
  Calendly, Microsoft Graph, Fireflies, `INTERNAL_TRIGGER_SECRET`, etc.)
  as a YAML file instead of one long `--set-env-vars` string. Build it
  from your `.env`:

  ```bash
  python3 -c "
  import re
  with open('.env') as f, open('.env.deploy.yaml', 'w') as out:
      for line in f:
          line = line.strip()
          if not line or line.startswith('#') or '=' not in line:
              continue
          key, _, val = line.partition('=')
          if key == 'LEADS_SHEET_PATH':
              continue  # overridden below — Cloud Run's local path is /data, not the one in .env
          val = val.strip().strip('\"')
          out.write(f'{key}: {val!r}\n')
      out.write(\"LEADS_SHEET_PATH: '/data/leads.xlsx'\n\")
  "
  ```

  **Don't commit `.env.deploy.yaml`** — it has the same secrets `.env`
  does. It's already covered by the same `.gitignore` pattern as `.env`
  if you're using git; if not, just delete it after the deploy succeeds
  and regenerate it next time.

The command prints a service URL when it finishes
(`https://calendly-briefing-system-xxxxx.us-central1.run.app`) — that's
what goes into Calendly's and Fireflies' webhook settings in step 4.

### 3. Create the two Cloud Scheduler jobs

Using the same secret you generated in step 2, and the service URL from
the deploy output:

```bash
SERVICE_URL="https://calendly-briefing-system-xxxxx.us-central1.run.app"
SECRET="the value you generated with openssl rand -hex 32"

gcloud scheduler jobs create http brief-scheduler-trigger \
    --location=us-central1 \
    --schedule="*/2 * * * *" \
    --uri="${SERVICE_URL}/internal/trigger/brief-scheduler" \
    --http-method=POST \
    --headers="X-Internal-Trigger-Secret=${SECRET}"

gcloud scheduler jobs create http live-catchup-trigger \
    --location=us-central1 \
    --schedule="*/5 * * * *" \
    --uri="${SERVICE_URL}/internal/trigger/live-catchup" \
    --http-method=POST \
    --headers="X-Internal-Trigger-Secret=${SECRET}"
```

Both schedules use standard cron syntax and run in UTC by default.

### 4. Point Calendly and Fireflies at the service

- Calendly webhook subscription → `${SERVICE_URL}/webhooks/calendly`
- Fireflies webhook (Developer Settings) → `${SERVICE_URL}/webhooks/fireflies`

### 5. Verify

```bash
curl "${SERVICE_URL}/healthz"                      # expect {"status": "ok"}
gcloud run services logs read calendly-briefing-system --region=us-central1
```

Then do the same real end-to-end test the local Setup section
recommends: book a real test consultation and confirm the brief lands
in the manager's Teams chat about 15 minutes before it, before relying
on this for real client bookings.

### Redeploying after a code change

```bash
gcloud run deploy calendly-briefing-system --source . --region=us-central1 --execution-environment=gen2
```

Cloud Run keeps the previous env vars, secrets, and volume mount from
the last deploy — you only need to pass them again if you're changing
one. The two Cloud Scheduler jobs and the Firestore indexes are
one-time setup and don't need to be recreated.

### Cost

At this app's volume (a small law firm's worth of consultations — think
tens per week, not thousands), this comfortably fits inside free
allowances on every piece: Cloud Run's free tier (2 million requests
and 180,000 vCPU-seconds/month), Firestore's free tier (50,000 reads
and 20,000 writes *per day*), Cloud Scheduler's free tier (3 jobs per
billing account — this uses exactly 2), and Cloud Storage's free tier
(5 GB-months, in the three eligible regions). The one caveat: all of
these Always Free allowances are shared *per billing account*, not per
project — if the same account is already running other Cloud Run
services, Firestore databases, or Cloud Scheduler jobs anywhere close
to those ceilings, this one could push it over. Worth a glance at
Billing → Reports after the first week running for real.

## Why a private chat, not the shared meeting chat (requirement 3)

Requirement 3 asked for the mid-call catch-up to go to "that particular
chat" — read literally, that could mean the Teams meeting's own shared
chat, which every participant (including the client) can see. That chat
also has the client in it. Posting an AI-generated summary of "what's
been discussed so far" into a chat the client can read raises an obvious
problem: it's a candid, possibly-incomplete AI paraphrase of a
privileged conversation, visible to the person the conversation is
about. This build deliberately routes the catch-up to the same private
manager-only chat the pre-call brief and post-meeting notes already use
instead. If the intent was actually the shared chat, that's a one-line
change in `app/live_catchup.py` (swap the `teams_delivery.send_manager_message`
call for a channel/chat-ID post) — but it should be a deliberate
decision, not a default.

Fireflies itself can't post into a Teams chat/DM either way — per their
own docs, "the Fireflies AI bot is used only for notifications — it
cannot engage in chat or @mentions within Teams" — so it's used purely
as the transcript/capture source, with Microsoft Graph handling all
message delivery. Clean separation: Fireflies watches and transcribes,
Graph delivers.

## Leads sheet (requirement 5)

Unlike the pre-call brief and notes, this isn't generated from a
template this code owns — it's designed to append into **your own**
Excel file, wherever it lives (a synced shared-drive folder —
OneDrive/Dropbox client, or a mapped network share). Point
`LEADS_SHEET_PATH` at it. Each new booking adds exactly one row with
Date, Time, Client Name, and Purpose, matched to whatever header wording
your file already uses (case-insensitive, tolerant of variations like
"Client" vs. "Client Name" or "Time of Meeting" vs. "Time") — see
`COLUMN_ALIASES` in `app/leads_sheet.py` if a column isn't being found
and needs a new alias added.

If nothing exists yet at `LEADS_SHEET_PATH`, a minimal default template
(exactly those four columns) is created automatically, so the system
works before the real file is in place. Once you drop in the real file
at the same path, run:

```bash
python -m app.leads_sheet --backfill
```

to add every booking made in the meantime — bookings are tracked
(`leads_sheet_synced_at`) so this is safe to re-run and never
duplicates a row.

The write is atomic (temp file + rename, under a file lock) so no one
ever opens a half-written file — but if the file is open and being
actively edited when a booking comes in, that edit and the new row can
conflict on save. Worth a note to whoever owns the file that rows get
added automatically in the background.

Set `LEADS_SHEET_ENABLED=false` to turn this off.

## Design notes / open assumptions

Worth manager/firm-lead sign-off before relying on this in production:

- **Attorney email = Microsoft 365 UPN.** The pipeline assumes the
  manager's Calendly host email matches their M365 sign-in (UPN), which
  Graph needs to resolve their 1:1 chat. If it differs, add an entry to
  `ATTORNEY_UPN_OVERRIDES` in `.env`.
- **Fireflies transcript-to-booking matching.** Fireflies has no native
  concept of a Calendly booking, so `webhook_receiver.py` matches a
  finished transcript to a booking by Teams join URL first, then falls
  back to the closest not-yet-synced booking for the same organizer.
  Worth spot-checking against real meetings early — if a firm runs back-
  to-back consultations with the same manager, the fallback match could
  pick the wrong one in rare cases.
- **Live-caption reliability during an in-progress meeting** (requirement
  3) is the one piece not yet verified against a real live consultation.
  Fireflies documents a live-caption API (`active_meetings` + live
  `sentences`) that this build relies on, but whether it populates
  promptly and completely mid-meeting across meeting lengths isn't
  something to assume without testing. `live_catchup.py` degrades
  gracefully either way — no content yet just means it checks again next
  tick — so worst case this feature quietly does nothing mid-meeting
  while the post-meeting notes (requirement 4) still catch everything
  afterward regardless.
- **Email-only client matching.** A client booking under a new email
  won't be flagged as a repeat client. `app/db.py::upsert_client` is the
  one place a name+phone fallback would plug in later, if that turns out
  to matter.
- **Calendly payload shape.** `app/webhook_receiver.py` and
  `app/calendly_client.py` are written against the current Calendly API
  v2 webhook/event shape — worth confirming against a real webhook
  delivery before go-live (test locally with `ngrok` and a real, or
  Calendly's test, booking).
- **Consent to recording/transcription.** Fireflies joining and
  transcribing consultations is a client-facing change (an AI notetaker
  in the meeting) that should have firm sign-off and, depending on
  jurisdiction, client notice/consent before enabling — that's a policy
  decision, not something this codebase enforces.

## What to revisit after running for a couple of weeks

- Confirm the Calendly payload and Fireflies-matching assumptions above
  against real deliveries.
- Confirm whether Fireflies' live captions actually populate promptly
  during a real in-progress meeting in this account — if they don't,
  `live_catchup.py` is harmless but ineffective, and the cron entry can
  be dropped without losing anything (post-meeting notes still work).
- Add alerting on repeated `brief_scheduler` / `live_catchup` / webhook
  failures (currently everything only logs; nothing pages anyone).
- Spot-check a sample of Groq-generated briefs/notes against what the
  manager would actually write, and watch logs for 429 (rate-limited)
  errors during peak booking days.
- Check in with whoever owns the leads spreadsheet on whether the column
  matching is working cleanly against their real file, and whether
  appending in real time (vs. some other cadence) is the right fit.
- Add the name+phone repeat-client fallback if new-email bookings turn
  out to be common.
