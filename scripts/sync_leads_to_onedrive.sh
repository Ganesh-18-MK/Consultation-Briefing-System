#!/bin/bash
# Pulls the latest leads sheet from the production Cloud Storage bucket
# and overwrites the local OneDrive-synced copy, so OneDrive picks up
# the change and syncs it up automatically. Run on a schedule (cron)
# on this Mac — Cloud Run itself has no way to write to a local file
# here directly, so this local pull-and-overwrite is the bridge.

SRC="gs://consultation-briefing-system-leads-sheet/leads.xlsx"
DEST="/Users/Ganesh/OneDrive - Mary Kennedy Law Offices/Calendly_Consultation_Clients.xlsx"
LOG="/Users/Ganesh/Projects/Calendly_Briefing_System/onedrive_sync.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Syncing $SRC -> $DEST" >> "$LOG"
/opt/homebrew/bin/gcloud storage cp "$SRC" "$DEST" >> "$LOG" 2>&1
if [ $? -eq 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync OK" >> "$LOG"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync FAILED (see above)" >> "$LOG"
fi
