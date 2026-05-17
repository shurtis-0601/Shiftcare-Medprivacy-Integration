"""
Rolling pipeline run log — appends a row per run to a local CSV and optionally
uploads it to a Google Drive audit folder after each run.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import date, datetime
from pathlib import Path

from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

_LOG_FILENAME = "pipeline_run_log.csv"
_COLUMNS = [
    "Run Date",
    "Run At (ISO)",
    "Notes Exported",
    "Notes Processed",
    "Notes Quarantined",
    "Notes Skipped",
    "Errors",
    "CSV File",
]
_MIME_CSV = "text/csv"


class RunLogger:
    def __init__(
        self,
        log_dir: Path,
        drive_audit_folder_id: str | None = None,
    ) -> None:
        self._log_path = Path(log_dir) / _LOG_FILENAME
        self._drive_folder_id = drive_audit_folder_id or os.environ.get("DRIVE_AUDIT_FOLDER_ID", "")
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self._ensure_header()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_run(
        self,
        run_date: date,
        run_at: datetime,
        notes_exported: int,
        notes_processed: int,
        notes_quarantined: int,
        notes_skipped: int,
        errors: list[str],
        csv_path: str = "",
    ) -> None:
        """Append one row to the local run log CSV."""
        row = [
            run_date.isoformat(),
            run_at.isoformat(),
            notes_exported,
            notes_processed,
            notes_quarantined,
            notes_skipped,
            "; ".join(errors) if errors else "",
            csv_path,
        ]
        with open(self._log_path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(row)
        logger.info("Run logged to %s", self._log_path)

    def upload_log(self, config) -> str | None:
        """
        Upload the run log CSV to the Drive audit folder.
        Returns the Drive file ID, or None if upload is skipped or fails.
        """
        folder_id = self._drive_folder_id
        if not folder_id:
            logger.info(
                "DRIVE_AUDIT_FOLDER_ID not set — skipping run log upload. "
                "Set this env var to enable automated audit log uploads."
            )
            return None

        try:
            creds, _ = google_auth_default(
                scopes=["https://www.googleapis.com/auth/drive.file"]
            )
            svc = build("drive", "v3", credentials=creds, cache_discovery=False)

            content = self._log_path.read_bytes()
            media = MediaIoBaseUpload(
                io.BytesIO(content),
                mimetype=_MIME_CSV,
                resumable=False,
            )

            # Check if a file with this name already exists in the folder so we
            # can update it in place rather than creating a duplicate.
            existing_id = self._find_existing(svc, folder_id, _LOG_FILENAME)

            if existing_id:
                file_ = (
                    svc.files()
                    .update(
                        fileId=existing_id,
                        media_body=media,
                        fields="id, name",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                file_id: str = file_["id"]
                logger.info("Updated run log in Drive: %s", file_id)
            else:
                metadata = {"name": _LOG_FILENAME, "parents": [folder_id]}
                file_ = (
                    svc.files()
                    .create(
                        body=metadata,
                        media_body=media,
                        fields="id, name",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                file_id = file_["id"]
                logger.info("Uploaded run log to Drive: %s", file_id)

            return file_id

        except HttpError as exc:
            logger.error("Failed to upload run log to Drive: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_header(self) -> None:
        if not self._log_path.exists():
            with open(self._log_path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(_COLUMNS)

    @staticmethod
    def _find_existing(svc, folder_id: str, filename: str) -> str | None:
        """Return the Drive file ID of an existing file with this name, or None."""
        try:
            q = (
                f"name = '{filename}' and "
                f"'{folder_id}' in parents and "
                f"trashed = false"
            )
            resp = (
                svc.files()
                .list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True)
                .execute()
            )
            files = resp.get("files", [])
            return files[0]["id"] if files else None
        except HttpError:
            return None
