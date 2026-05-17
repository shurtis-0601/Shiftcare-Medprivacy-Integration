"""
Configuration loader. Reads from environment variables for local use,
falls back to Google Secret Manager for Cloud Function deployment.
"""
import os
from google.cloud import secretmanager


def _fetch_secret(project_id: str, secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8").strip()


class Config:
    def __init__(self):
        self.project_id: str = os.environ["GCP_PROJECT_ID"]

        # ShiftCare
        self.shiftcare_base_url: str = os.environ.get(
            "SHIFTCARE_BASE_URL", "https://app.shiftcare.com"
        )
        self.shiftcare_api_key: str = self._resolve(
            "SHIFTCARE_API_KEY", "shiftcare-api-key"
        )

        # Google Drive folder IDs
        self.drive_pending_folder_id: str = os.environ["DRIVE_PENDING_FOLDER_ID"]
        self.drive_quarantine_folder_id: str = os.environ["DRIVE_QUARANTINE_FOLDER_ID"]

        # Google Sheets
        self.reference_map_sheet_id: str = os.environ["REFERENCE_MAP_SHEET_ID"]

        # Notifications
        self.notification_email: str = os.environ["NOTIFICATION_EMAIL"]
        # Email address the service account impersonates to send mail (requires DWD).
        # If blank, notification falls back to Cloud Logging only.
        self.gmail_sender: str = os.environ.get("GMAIL_SENDER_EMAIL", "")

        # GCP
        self.dlp_location: str = os.environ.get("DLP_LOCATION", "global")
        self.timezone: str = os.environ.get("TIMEZONE", "Australia/Melbourne")

        # How many DLP findings of LIKELY+ are acceptable before quarantining.
        # 0 = zero tolerance (recommended).
        self.quarantine_threshold: int = int(
            os.environ.get("QUARANTINE_FINDING_THRESHOLD", "0")
        )

    def _resolve(self, env_var: str, secret_id: str) -> str:
        """Return env var value if set (local dev), otherwise pull from Secret Manager."""
        val = os.environ.get(env_var)
        if val:
            return val
        return _fetch_secret(self.project_id, secret_id)
