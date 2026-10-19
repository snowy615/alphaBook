"""
One-time OAuth consent flow for the site's Google connections: app/gcal.py's
Google Meet links, and sending the site's email as that account
(app/mailer.py).

There are two connections, both made with this script and the same OAuth
client:

  * the main one, admin@alphabook.uk: Meet links and email
        python scripts/gcal_oauth_setup.py --client-id ... --client-secret ...
  * the backup sender, admin@oxfordalphafund.com: email only, used when the
    main one can't send
        python scripts/gcal_oauth_setup.py --client-id ... --client-secret ... --backup

It opens a browser tab; sign in as the account named, and allow both
permissions. It then prints the env vars to set on Cloud Run. Nobody needs to
run it again unless a refresh token is revoked.

Before the first run, in Google Cloud Console for this project:
  1. APIs & Services -> Library -> enable "Google Calendar API" and "Gmail API".
  2. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID.
     - Application type: Desktop app.
     - Note the Client ID and Client Secret it gives you.

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
# Calendar events mint the Meet links; gmail.send lets the site send its
# emails as this same account (app/mailer.py) instead of via SMTP.
SCOPE = " ".join([
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
])
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
MAIN_ACCOUNT = "admin@alphabook.uk"
BACKUP_ACCOUNT = "admin@oxfordalphafund.com"

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
    parser.add_argument("--backup", action="store_true",
                        help="connect the backup sender (admin@oxfordalphafund.com) instead of the main account")
    parser.add_argument("--save-to", metavar="FILE",
                        help="write the env vars to FILE (readable only by you) instead of printing them; "
                             "scripts/connect_google.sh uses this so the token is never shown")
    args = parser.parse_args()
    account = BACKUP_ACCOUNT if args.backup else MAIN_ACCOUNT

    # Threaded: browsers often open a spare "preconnect" socket and leave it
    # idle. A single-threaded server blocks on it, so shutdown() below never
    # returns and the script hangs after "you may close this window".
    server = http.server.ThreadingHTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.daemon_threads = True
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
        # Picks the right account in the browser's account chooser.
        "login_hint": account,
    })
    print(f"Sign in as {account}.")
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

    resp = httpx.post(TOKEN_URL, timeout=30, data={
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
            f"https://myaccount.google.com/permissions (for {account}) and run this again."
        )

    env = {"GOOGLE_OAUTH_CLIENT_ID": args.client_id, "GOOGLE_OAUTH_CLIENT_SECRET": args.client_secret}
    if args.backup:
        env.update(GOOGLE_BACKUP_ACCOUNT_EMAIL=account, GOOGLE_BACKUP_REFRESH_TOKEN=refresh_token)
    else:
        env.update(GOOGLE_ACCOUNT_EMAIL=account, GOOGLE_OAUTH_REFRESH_TOKEN=refresh_token)
    if args.save_to:
        import os
        fd = os.open(args.save_to, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(f"{k}={v}\n" for k, v in env.items())
        print(f"\nSuccess, connected {account}.")
        return
    print(f"\nSuccess, connected {account}. Set these as Cloud Run env vars:\n")
    for k, v in env.items():
        print(f"{k}={v}")

if __name__ == "__main__":
    main()
