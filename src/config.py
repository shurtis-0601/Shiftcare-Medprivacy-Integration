"""
Configuration loader. Reads all values from environment variables.
Use a .env file with python-dotenv (or export vars in your shell) for local runs.
"""
import os


class Config:
    def __init__(self):
        self.project_id: str = os.environ["GCP_PROJECT_ID"]

        # ShiftCare
        self.shiftcare_base_url: str = os.environ.get(
            "SHIFTCARE_BASE_URL", "https://app.shiftcare.com"
        )
        self.shiftcare_api_key: str = os.environ["SHIFTCARE_API_KEY"]

        # Google Drive folder IDs
        self.drive_pending_folder_id: str = os.environ["DRIVE_PENDING_FOLDER_ID"]
        self.drive_quarantine_folder_id: str = os.environ["DRIVE_QUARANTINE_FOLDER_ID"]

        # Google Sheets
        self.reference_map_sheet_id: str = os.environ["REFERENCE_MAP_SHEET_ID"]

        # Notifications
        self.notification_email: str = os.environ["NOTIFICATION_EMAIL"]
        self.gmail_sender: str = os.environ.get("GMAIL_SENDER_EMAIL", "")

        # OAuth2 desktop credentials
        self.oauth_client_secrets_path: str = os.environ.get(
            "OAUTH_CLIENT_SECRETS_PATH", "client_secrets.json"
        )
        self.oauth_token_path: str = os.environ.get("OAUTH_TOKEN_PATH", "token.json")

        # GCP
        self.dlp_location: str = os.environ.get("DLP_LOCATION", "global")
        self.timezone: str = os.environ.get("TIMEZONE", "Australia/Melbourne")

        self.quarantine_threshold: int = int(
            os.environ.get("QUARANTINE_FINDING_THRESHOLD", "0")
        )
