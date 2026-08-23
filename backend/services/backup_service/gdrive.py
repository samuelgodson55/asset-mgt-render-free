"""
services/backup/gdrive.py
----------------------------
Google Drive upload for durability across Render Free's ephemeral disk
(see the package docstring). Supports OAuth-as-a-user and service-account
auth modes -- see config.py's BACKUP_GDRIVE_* docstring for why both exist.
"""

from __future__ import annotations

import json

from config import settings


def upload_to_gdrive(filepath: str, filename: str) -> str:
    """
    Uploads `filepath` into settings.BACKUP_GDRIVE_FOLDER_ID and returns the
    new Drive file's ID. Supports TWO auth modes -- see config.py's
    BACKUP_GDRIVE_* docstring for the full explanation of why there are two:

      1. OAuth as a real Google user (BACKUP_GDRIVE_OAUTH_*) -- required
         for a personal/consumer Google account. Tried FIRST if configured.
      2. A service account (BACKUP_GDRIVE_CREDENTIALS_JSON) -- only works
         if BACKUP_GDRIVE_FOLDER_ID is a Shared Drive folder (Google
         Workspace only). Used as a fallback if mode 1 isn't configured.

    Raises a clear, mode-specific error if neither is configured, or if the
    configured mode is itself missing a required value.
    """
    if not settings.BACKUP_GDRIVE_FOLDER_ID:
        raise RuntimeError("BACKUP_GDRIVE_FOLDER_ID is not set -- add the destination Drive folder's ID.")

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive libraries aren't installed -- add google-api-python-client, "
            "google-auth, and google-auth-httplib2 to backend/requirements.txt."
        ) from exc

    credentials = _build_gdrive_credentials()
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

    file_metadata = {"name": filename, "parents": [settings.BACKUP_GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(filepath, mimetype="application/gzip", resumable=False)
    created = drive.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return created["id"]

def _build_gdrive_credentials():
    """
    Returns a google-auth Credentials object for whichever mode is
    configured, OAuth-as-a-user taking priority over the service account
    (see upload_to_gdrive()'s docstring). Raises a RuntimeError naming
    exactly which setting is missing if the chosen mode is incomplete,
    rather than letting google-auth raise its own less-obvious error.
    """
    oauth_configured = bool(
        settings.BACKUP_GDRIVE_OAUTH_CLIENT_ID
        or settings.BACKUP_GDRIVE_OAUTH_CLIENT_SECRET
        or settings.BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN
    )

    if oauth_configured:
        missing = [
            name for name, value in (
                ("BACKUP_GDRIVE_OAUTH_CLIENT_ID", settings.BACKUP_GDRIVE_OAUTH_CLIENT_ID),
                ("BACKUP_GDRIVE_OAUTH_CLIENT_SECRET", settings.BACKUP_GDRIVE_OAUTH_CLIENT_SECRET),
                ("BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN", settings.BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                f"Google Drive OAuth is only partially configured -- missing {', '.join(missing)}. "
                "Run backend/scripts/gdrive_oauth_setup.py to generate all three values."
            )

        from google.oauth2.credentials import Credentials
        # `token=None` is fine -- google-auth fetches a fresh access token
        # from the refresh_token automatically on first use (and again
        # whenever it expires), no manual refresh() call needed here.
        return Credentials(
            token=None,
            refresh_token=settings.BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN,
            client_id=settings.BACKUP_GDRIVE_OAUTH_CLIENT_ID,
            client_secret=settings.BACKUP_GDRIVE_OAUTH_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )

    if settings.BACKUP_GDRIVE_CREDENTIALS_JSON:
        from google.oauth2 import service_account
        credentials_info = json.loads(settings.BACKUP_GDRIVE_CREDENTIALS_JSON)
        return service_account.Credentials.from_service_account_info(
            credentials_info, scopes=["https://www.googleapis.com/auth/drive.file"]
        )

    raise RuntimeError(
        "No Google Drive credentials configured -- set either BACKUP_GDRIVE_OAUTH_CLIENT_ID/"
        "_CLIENT_SECRET/_REFRESH_TOKEN (personal Google account -- see backend/scripts/"
        "gdrive_oauth_setup.py) or BACKUP_GDRIVE_CREDENTIALS_JSON (Workspace service account)."
    )
