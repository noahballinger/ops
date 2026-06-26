"""
Google OAuth — ONE app, BOTH Gmail-send and Drive scopes (per the locked
decision). Desktop/loopback flow, so no public exposure needed.

Authorize once on the host machine:
    python -m app.google_oauth        # opens a browser, writes google_token.json

Migrating to a Workspace account later = delete the token, re-run this, done.
Client secret + token live in backend/data/ (gitignored), paths overridable via
GOOGLE_CLIENT_SECRET_FILE / GOOGLE_TOKEN_FILE.
"""
from __future__ import annotations

import json
import os

# Relax scope-order checks (Google reorders / adds 'openid'), avoids spurious
# "Scope has changed" errors during the login code exchange.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",
]
# Identity-only scopes for "Sign in with Google" (app login).
LOGIN_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile"]
_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
CLIENT_SECRET_FILE = os.environ.get(
    "GOOGLE_CLIENT_SECRET_FILE", os.path.join(_DATA, "google_client_secret.json"))
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", os.path.join(_DATA, "google_token.json"))


def load_creds():
    """Return valid Credentials (refreshing if needed) or None if not authorized."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except Exception:
        return None
    if not os.path.exists(TOKEN_FILE):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
    return creds if creds and creds.valid else None


def authorize():
    """Interactive consent on the host (loopback). Writes the token file."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not os.path.exists(CLIENT_SECRET_FILE):
        raise SystemExit(f"Missing client secret at {CLIENT_SECRET_FILE}")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds)
    print(f"Authorized. Token written to {TOKEN_FILE}")


def _save(creds):
    os.makedirs(_DATA, exist_ok=True)
    with open(TOKEN_FILE, "w") as fh:
        fh.write(creds.to_json())
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def client_id() -> str:
    try:
        with open(CLIENT_SECRET_FILE) as fh:
            d = json.load(fh)
        return (d.get("installed") or d.get("web") or {}).get("client_id", "")
    except Exception:
        return ""


def login_flow(redirect_uri: str):
    """Web OAuth flow for app login (identity scopes only)."""
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE, scopes=LOGIN_SCOPES, redirect_uri=redirect_uri)


def verify_login_id_token(raw_id_token: str) -> dict:
    """Return the verified id-token claims (email, name, …)."""
    from google.oauth2 import id_token
    from google.auth.transport import requests as greq
    return id_token.verify_oauth2_token(raw_id_token, greq.Request(),
                                        audience=client_id() or None)


def login_allowed(email: str) -> bool:
    """Optional gate: ALLOWED_GOOGLE_EMAILS (csv) / ALLOWED_GOOGLE_DOMAIN.
    If neither is set, any successful Google sign-in is allowed (the consent
    screen already restricts to test users while the app is unpublished)."""
    email = (email or "").lower()
    emails = [e.strip().lower() for e in
              os.environ.get("ALLOWED_GOOGLE_EMAILS", "").split(",") if e.strip()]
    domain = os.environ.get("ALLOWED_GOOGLE_DOMAIN", "").strip().lower().lstrip("@")
    if not emails and not domain:
        return True
    if emails and email in emails:
        return True
    if domain and email.endswith("@" + domain):
        return True
    return False


def status() -> dict:
    have_secret = os.path.exists(CLIENT_SECRET_FILE)
    creds = load_creds()
    return {"client_configured": have_secret,
            "authorized": creds is not None,
            "scopes": SCOPES}


if __name__ == "__main__":
    authorize()
