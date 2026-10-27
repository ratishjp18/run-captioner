"""
One-time setup script. Run this before starting the server.

It does two things:
  1. Walks you through the Strava OAuth flow to obtain a refresh token.
  2. Registers (or checks) the Strava webhook subscription.

Usage
-----
  # Full setup (OAuth + webhook):
  python setup_oauth.py --callback-url https://your-server.com/webhook

  # Register webhook only (if you already have tokens in .env):
  python setup_oauth.py --callback-url https://your-server.com/webhook --skip-oauth

  # Check existing webhook subscription:
  python setup_oauth.py --callback-url https://example.com/webhook --list-only
"""

import argparse
import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv

load_dotenv()

LOCAL_PORT = 8888
REDIRECT_URI = f"http://localhost:{LOCAL_PORT}/callback"

# ── OAuth helpers ─────────────────────────────────────────────────────────────

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Tiny local HTTP server that captures the OAuth callback code."""
    auth_code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _OAuthCallbackHandler.auth_code = params["code"][0]
            self._respond(200, b"<h1>Authorized! You can close this tab.</h1>")
        elif "error" in params:
            _OAuthCallbackHandler.error = params["error"][0]
            self._respond(400, b"<h1>Authorization denied.</h1>")
        else:
            self._respond(400, b"<h1>Unknown callback.</h1>")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # suppress server logs
        pass


def _get_auth_url(client_id: str) -> str:
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "activity:read_all,activity:write",
        "approval_prompt": "auto",
    })
    return f"https://www.strava.com/oauth/authorize?{params}"


def _exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    resp = httpx.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


# ── Webhook helpers ───────────────────────────────────────────────────────────

def _list_subscriptions(client_id: str, client_secret: str) -> list[dict]:
    resp = httpx.get(
        "https://www.strava.com/api/v3/push_subscriptions",
        params={"client_id": client_id, "client_secret": client_secret},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _create_subscription(
    client_id: str, client_secret: str, callback_url: str, verify_token: str
) -> dict:
    resp = httpx.post(
        "https://www.strava.com/api/v3/push_subscriptions",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "callback_url": callback_url,
            "verify_token": verify_token,
        },
        timeout=20,
    )
    if resp.status_code == 409:
        print("⚠  A webhook is already registered. Fetching existing subscription...")
        return _list_subscriptions(client_id, client_secret)
    resp.raise_for_status()
    return resp.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strava OAuth setup and webhook registration for run-titler"
    )
    parser.add_argument(
        "--callback-url",
        required=True,
        help="Public HTTPS URL for the /webhook endpoint (e.g. https://abc.ngrok.io/webhook)",
    )
    parser.add_argument(
        "--skip-oauth",
        action="store_true",
        help="Skip the OAuth flow; just register or check the webhook",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List existing webhook subscriptions and exit",
    )
    args = parser.parse_args()

    client_id = os.environ.get("STRAVA_CLIENT_ID", "")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "")
    verify_token = os.environ.get("STRAVA_VERIFY_TOKEN") or secrets.token_hex(16)

    if not client_id or not client_secret:
        print("ERROR: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    # ── List only ─────────────────────────────────────────────────────────────
    if args.list_only:
        subs = _list_subscriptions(client_id, client_secret)
        print("Existing webhook subscriptions:")
        for s in subs:
            print(f"  id={s.get('id')}  callback_url={s.get('callback_url')}")
        return

    # ── OAuth flow ────────────────────────────────────────────────────────────
    if not args.skip_oauth:
        print("\n=== Step 1: Strava OAuth ===")
        auth_url = _get_auth_url(client_id)
        print(f"\nOpening browser for Strava authorization...")
        print(f"URL: {auth_url}\n")
        webbrowser.open(auth_url)

        print(f"Waiting for callback on http://localhost:{LOCAL_PORT}/callback ...")
        server = HTTPServer(("localhost", LOCAL_PORT), _OAuthCallbackHandler)

        while _OAuthCallbackHandler.auth_code is None and _OAuthCallbackHandler.error is None:
            server.handle_request()

        if _OAuthCallbackHandler.error:
            print(f"\nERROR: Authorization denied: {_OAuthCallbackHandler.error}")
            sys.exit(1)

        code = _OAuthCallbackHandler.auth_code
        print("\nExchanging authorization code for tokens...")

        try:
            tokens = _exchange_code(client_id, client_secret, code)
        except httpx.HTTPStatusError as exc:
            print(f"ERROR: Token exchange failed: {exc.response.text}")
            sys.exit(1)

        athlete = tokens.get("athlete", {})
        refresh_token = tokens.get("refresh_token", "")
        athlete_id = athlete.get("id", "")
        athlete_name = athlete.get("firstname", "") + " " + athlete.get("lastname", "")

        print(f"\nAuthenticated as: {athlete_name.strip()} (ID: {athlete_id})")
        print("\n=== Add these values to your .env file ===")
        print(f"STRAVA_REFRESH_TOKEN={refresh_token}")
        print(f"ATHLETE_ID={athlete_id}")
        print(f"STRAVA_VERIFY_TOKEN={verify_token}")
        print("==========================================\n")

    # ── Webhook registration ──────────────────────────────────────────────────
    print("=== Step 2: Register Strava webhook ===")
    print(f"Callback URL : {args.callback_url}")
    print(f"Verify token : {verify_token}")
    print()

    if not args.callback_url.startswith("https://"):
        print("WARNING: Strava requires an HTTPS callback URL.")

    try:
        result = _create_subscription(
            client_id, client_secret, args.callback_url, verify_token
        )
        print(f"Webhook registration result: {result}")
        print("\n✓ Setup complete. Start the server with: uvicorn main:app")
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: Webhook registration failed: {exc.response.text}")
        print(
            "\nMake sure your server is running and reachable at the callback URL "
            "before registering."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
