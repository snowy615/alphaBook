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
import time
import urllib.parse
import webbrowser

import httpx

# Google's "confirm it's you" re-sign-in can easily take a few minutes, so wait
# a generous while — and if the redirect still arrives too late, the code can
# be pasted in by hand (see main()).
WAIT_SECONDS = 15 * 60
REDIRECT_PORT = 8765
# 127.0.0.1 rather than "localhost": browsers may resolve localhost to IPv6
# (::1) while this server listens on IPv4, which shows as "refused to connect".
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_result: dict = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib method name)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # Only the OAuth redirect counts — anything else (a favicon request,
        # a stray reload) is answered and ignored rather than ending the wait.
        if "code" in qs or "error" in qs:
            _result["code"] = qs.get("code", [None])[0]
            _result["error"] = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        message = ("Signed in — you may close this window." if _result.get("code")
                   else "Something went wrong — check the terminal.")
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode())

    def log_message(self, *args):
        pass   # keep the terminal clean


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

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

    print(f"Waiting up to {WAIT_SECONDS // 60} minutes for you to finish signing in…")
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline and not (_result.get("code") or _result.get("error")):
        time.sleep(0.5)
    server.shutdown()

    if _result.get("error"):
        raise SystemExit(f"Google returned an error: {_result['error']}. Try again.")
    if not _result.get("code"):
        # The redirect never reached this script (it timed out, or the browser
        # couldn't connect). Google still put the code in the address bar of
        # the page it redirected to, so it can be pasted in by hand. Codes
        # only last a few minutes, so do this promptly.
        pasted = input(
            "\nDidn't hear back from the browser. If you finished signing in, copy the full\n"
            "address from the browser tab it ended on (starts with http://127.0.0.1:8765/?)\n"
            "and paste it here, then press Enter:\n> "
        ).strip()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [None])[0]
        if not code:
            raise SystemExit("That address has no code in it. Run the script again.")
        _result["code"] = code

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
