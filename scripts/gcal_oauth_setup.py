"""
One-time OAuth consent flow for app/gcal.py's Google Meet link generation.

Run this once, locally, signed into oxfordalphafund@gmail.com in the browser
it opens. It prints a refresh token — set that (plus the client id/secret)
as Cloud Run env vars and app/gcal.py can start minting Meet links for
interviews. Nobody else needs to run this again unless the refresh token is
revoked.

Before running, in Google Cloud Console for this project:
  1. APIs & Services -> Library -> enable "Google Calendar API".
  2. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID.
     - Application type: Desktop app.
     - Note the Client ID and Client Secret it gives you.
  3. Run this script with those two values:
       python scripts/gcal_oauth_setup.py --client-id ... --client-secret ...
  4. It opens a browser tab. Sign in as oxfordalphafund@gmail.com and grant
     calendar access. The tab will say "you may close this window" once done.
  5. Copy the refresh token this script prints, then set all three as
     Cloud Run env vars:

       gcloud run services update alphabook-api --region us-central1 \\
         --update-env-vars GOOGLE_OAUTH_CLIENT_ID=...,GOOGLE_OAUTH_CLIENT_SECRET=...,GOOGLE_OAUTH_REFRESH_TOKEN=...

No dependency beyond the stdlib and httpx (already required by the app).
"""
from __future__ import annotations

import argparse
import http.server
import threading
import urllib.parse
import webbrowser

import httpx

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_result: dict = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib method name)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _result["code"] = qs.get("code", [None])[0]
        _result["error"] = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        message = "Signed in — you may close this window." if _result["code"] else "Something went wrong — check the terminal."
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode())

    def log_message(self, *args):
        pass   # keep the terminal clean


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Forces Google to hand back a refresh token even if this app was
        # authorized before — without this a repeat run can silently get
        # none back at all.
        "prompt": "consent",
    })
    print(f"Opening your browser to sign in — if it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=180)
    if not _result.get("code"):
        raise SystemExit(f"No authorization code received (error: {_result.get('error')}). Try again.")

    resp = httpx.post(TOKEN_URL, data={
        "code": _result["code"],
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "Google didn't return a refresh token. This usually means the app was already "
            "authorized before without --prompt=consent taking effect — revoke access at "
            "https://myaccount.google.com/permissions (for oxfordalphafund@gmail.com) and run this again."
        )

    print("\nSuccess. Set these as Cloud Run env vars:\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={args.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={args.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}")


if __name__ == "__main__":
    main()
