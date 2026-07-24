"""
backend/scripts/gdrive_oauth_setup.py
--------------------------------------
Run this ONCE, on your own machine (NOT inside Docker), to authorize Google
Drive backups against your PERSONAL Google account instead of a Workspace
service account. It opens a browser, has you log in and approve access, and
prints the three .env values you need. Nothing about your Snipe-IT Lite
data or database is touched by this script -- it only talks to Google.

WHY THIS EXISTS
A Google Cloud "service account" (the original BACKUP_GDRIVE_CREDENTIALS_
JSON approach) has ZERO Drive storage quota of its own. Even a folder you
personally share with it as "Editor" doesn't help -- files it creates are
still owned BY the service account and billed against its (nonexistent)
quota, which is exactly the "Service Accounts do not have storage quota...
storageQuotaExceeded" error you get on a personal/consumer Gmail account
(the one place that's billed differently -- a Shared Drive -- is a Google
Workspace-only feature). The fix is to authenticate as YOURSELF instead, so
uploads count against your own normal 15GB Drive quota, exactly like
uploading through drive.google.com by hand.

ONE-TIME SETUP (about 5 minutes):
  1. Go to https://console.cloud.google.com/ and create a project (or reuse
     one) -- this is free, no billing account required for this.
  2. APIs & Services -> Library -> enable the "Google Drive API".
  3. APIs & Services -> OAuth consent screen -> choose "External" ->
     fill in an app name/support email -> Save. You do NOT need to submit
     this for verification -- it's fine to leave it in "Testing" mode
     forever for personal use like this.
     -> On the "Test users" step, add your own Google account's email.
  4. APIs & Services -> Credentials -> Create Credentials -> OAuth client
     ID -> Application type: "Desktop app" -> Create. Download the JSON
     file it gives you (the "Download JSON" button).
  5. Install the one extra dependency this script needs (NOT required by
     the running app/container, only by this one-time script):
       pip install google-auth-oauthlib
  6. Run this script, pointing it at the JSON file you downloaded:
       python gdrive_oauth_setup.py /path/to/client_secret_....json
     A browser window opens -- log in with the SAME Google account whose
     Drive storage you want backups to live in, and click Allow.
  7. Copy the three lines this script prints into your .env file, e.g.:
       BACKUP_GDRIVE_OAUTH_CLIENT_ID=...
       BACKUP_GDRIVE_OAUTH_CLIENT_SECRET=...
       BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN=...
  8. Create (or pick) a normal folder in your own Google Drive for backups
     to land in, grab its ID from the folder's URL
     (https://drive.google.com/drive/folders/<THIS_PART>), and set
     BACKUP_GDRIVE_FOLDER_ID=<that ID> in .env too. No sharing step needed
     this time -- you're uploading as yourself.
  9. Set BACKUP_GDRIVE_ENABLED=true, restart the backend, and try
     "Backup Now" from the System Backups panel.

The refresh token this prints does not expire from time passing alone (it
can be revoked manually any time from https://myaccount.google.com/permissions,
or by Google if unused for 6 months) -- rerun this script if that happens.
"""

import sys

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} /path/to/client_secret_....json")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing dependency -- run: pip install google-auth-oauthlib")
        sys.exit(1)

    client_secret_path = sys.argv[1]
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    # Spins up a temporary local server, opens your default browser to
    # Google's consent screen, and waits for the redirect back -- the
    # standard "installed app" OAuth flow, no manual code-pasting needed.
    credentials = flow.run_local_server(port=0)

    print("\nSuccess! Add these three lines to your .env file:\n")
    print(f"BACKUP_GDRIVE_OAUTH_CLIENT_ID={credentials.client_id}")
    print(f"BACKUP_GDRIVE_OAUTH_CLIENT_SECRET={credentials.client_secret}")
    print(f"BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN={credentials.refresh_token}")
    print(
        "\nThen set BACKUP_GDRIVE_FOLDER_ID to a folder in your own Drive "
        "and BACKUP_GDRIVE_ENABLED=true, and restart the backend."
    )


if __name__ == "__main__":
    main()
