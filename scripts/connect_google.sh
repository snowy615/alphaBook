#!/usr/bin/env bash
# Connect one of the site's Google mailboxes and save it straight to Cloud Run,
# without the refresh token ever being shown on screen.
#
#   scripts/connect_google.sh            # admin@alphabook.uk: Meet links + email
#   scripts/connect_google.sh --backup   # admin@oxfordalphafund.com: backup email sender
#
# Opens a browser tab: sign in as the account it names and click Allow. Uses
# the OAuth client already configured on the alphabook-api Cloud Run service.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=alphabook-5ef4e
REGION=us-central1
SERVICE=alphabook-api

env_of() {
  gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format=json \
    | python3 -c "import json,sys; e={x['name']:x.get('value','') for x in json.load(sys.stdin)['spec']['template']['spec']['containers'][0]['env']}; print(e.get('$1',''))"
}

CLIENT_ID=$(env_of GOOGLE_OAUTH_CLIENT_ID)
CLIENT_SECRET=$(env_of GOOGLE_OAUTH_CLIENT_SECRET)
if [[ -z "$CLIENT_ID" || -z "$CLIENT_SECRET" ]]; then
  echo "Cloud Run has no GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET to use." >&2
  exit 1
fi

OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT
venv/bin/python scripts/gcal_oauth_setup.py --client-id "$CLIENT_ID" --client-secret "$CLIENT_SECRET" --save-to "$OUT" "$@"

UPDATES=$(grep -E '^GOOGLE_(ACCOUNT_EMAIL|OAUTH_REFRESH_TOKEN|BACKUP_ACCOUNT_EMAIL|BACKUP_REFRESH_TOKEN)=' "$OUT" | paste -sd, -)
echo "Saving to Cloud Run (a new revision rolls out in about a minute)…"
gcloud run services update "$SERVICE" --region "$REGION" --project "$PROJECT" --quiet --update-env-vars "$UPDATES" >/dev/null
echo "Done: Cloud Run now uses this connection."
