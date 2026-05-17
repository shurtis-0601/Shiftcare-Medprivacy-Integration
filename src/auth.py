"""
Google OAuth2 desktop app authentication.

First run: opens a browser for user consent and saves the token to token.json.
Subsequent runs: loads and silently refreshes the token from token.json.
"""
from __future__ import annotations

import os

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_credentials(
    client_secrets_path: str | None = None,
    token_path: str | None = None,
) -> Credentials:
    """Return valid OAuth2 credentials, running the browser flow on first use."""
    client_secrets_path = client_secrets_path or os.environ.get(
        "OAUTH_CLIENT_SECRETS_PATH", "client_secrets.json"
    )
    token_path = token_path or os.environ.get("OAUTH_TOKEN_PATH", "token.json")

    creds: Credentials | None = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, _SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as fh:
            fh.write(creds.to_json())

    return creds
