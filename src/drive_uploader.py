"""
Google Drive uploader.

Creates plain-text files in the specified Google Drive folder.
Uses the Drive API v3 with a service account (Application Default Credentials).
"""
from __future__ import annotations

import io
import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from google.auth import default as google_auth_default

logger = logging.getLogger(__name__)

_MIME_TEXT = "text/plain"
_MIME_FOLDER = "application/vnd.google-apps.folder"


class DriveUploader:
    def __init__(self, config) -> None:
        self._pending_folder_id = config.drive_pending_folder_id
        self._quarantine_folder_id = config.drive_quarantine_folder_id

        creds, _ = google_auth_default(
            scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def upload_to_pending(self, content: str, filename: str) -> str:
        """Upload a de-identified note to the Pending folder. Returns the Drive file ID."""
        return self._upload(content, filename, self._pending_folder_id)

    def upload_to_quarantine(self, content: str, filename: str) -> str:
        """Upload a quarantined note to the Quarantine folder. Returns the Drive file ID."""
        return self._upload(content, filename, self._quarantine_folder_id)

    def _upload(self, content: str, filename: str, folder_id: str) -> str:
        metadata = {"name": filename, "parents": [folder_id]}
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype=_MIME_TEXT,
            resumable=False,
        )
        try:
            file_ = (
                self._svc.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id, name",
                    supportsAllDrives=True,
                )
                .execute()
            )
            file_id: str = file_["id"]
            logger.info("Uploaded '%s' → Drive ID %s (folder %s)", filename, file_id, folder_id)
            return file_id
        except HttpError as exc:
            logger.error("Drive upload failed for '%s': %s", filename, exc)
            raise
