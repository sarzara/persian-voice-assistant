#!/usr/bin/env python3
"""One-time Google OAuth 2.0 consent for the owner's calendar. Prints a refresh token.

Run on a computer with a browser (NOT inside Docker). Standard library only:

    python scripts/google_auth.py                      # reads GOOGLE_CLIENT_ID/SECRET from env or ./.env
    python scripts/google_auth.py --client-id ... --client-secret ...

Flow: loopback redirect (http://127.0.0.1:<random port>/) with PKCE, which is
what Google recommends for "Desktop app" OAuth clients. Scope: calendar.events.
Copy the printed value into .env as GOOGLE_REFRESH_TOKEN. Treat it like a password.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.events"


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


class _Handler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        _Handler.result = query
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Done. You can close this tab and return to the terminal." if "code" in query else f"Error: {query.get('error')}"
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args):  # silence request logging
        pass


def main() -> int:
    dotenv = read_dotenv(Path(".env"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default=os.environ.get("GOOGLE_CLIENT_ID") or dotenv.get("GOOGLE_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("GOOGLE_CLIENT_SECRET") or dotenv.get("GOOGLE_CLIENT_SECRET"))
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    args = parser.parse_args()
    if not args.client_id or not args.client_secret or "replace-me" in (args.client_id + args.client_secret):
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (env, .env, or --client-id/--client-secret).", file=sys.stderr)
        return 2

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",        # always return one, even if consented before
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    print("Opening the Google consent page. If it does not open, visit:\n\n" + url + "\n", flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    # Serve until the redirect with ?code= (or ?error=) arrives; ignore stray requests like /favicon.ico.
    while not _Handler.result:
        server.handle_request()
    server.server_close()

    result = _Handler.result
    if result.get("state") != state:
        print("State mismatch: possible CSRF, aborting.", file=sys.stderr)
        return 1
    if "error" in result:
        print(f"Authorization failed: {result['error']}", file=sys.stderr)
        return 1

    data = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as resp:
            tokens = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"Token exchange failed: {exc.code} {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 1
    if SCOPE not in tokens.get("scope", ""):
        print(f"Warning: granted scopes are {tokens.get('scope')!r}; calendar.events was not granted.", file=sys.stderr)
    print("Success. Add this line to your .env (keep it secret):\n")
    print(f"GOOGLE_REFRESH_TOKEN={refresh}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
