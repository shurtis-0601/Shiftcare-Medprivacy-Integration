"""
Downloads the master PII CSV from Google Drive and parses it into
supplementary name/org/location sets for the de-identification engine.

Expected CSV format (same as medprivacy.py pii_database.csv):
    Type,Name
    Participant,Jane Smith
    Provider,Dr. Alex Worker
    Organization,Monash Health
    Location,42 Example Street Melbourne
    Carer,Margaret Smith

The pipeline already pulls Participant and Provider data from ShiftCare's API,
so those rows are parsed but treated as a secondary / cross-check source.
The main value of the CSV is Carers, Organizations, and Locations that
ShiftCare's API does not expose.

Set PII_CSV_FILE_ID in your environment to the Google Drive file ID of the CSV.
If not set, this step is skipped silently.
"""
from __future__ import annotations

import csv
import io
import logging
import os

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth import default as google_auth_default

logger = logging.getLogger(__name__)

# Re-use the name-variation generator from the deidentifier
from src.deidentifier import generate_name_variations


class SupplementaryPII:
    """Holds the additional PII sets loaded from the master CSV."""

    def __init__(self) -> None:
        self.carers: set[str] = set()
        self.organizations: set[str] = set()
        self.locations: set[str] = set()
        # Providers / participants from CSV are kept for cross-reference
        # but the authoritative source is ShiftCare's API.
        self.extra_providers: set[str] = set()
        self.extra_participants: set[str] = set()

    @property
    def is_empty(self) -> bool:
        return not any([
            self.carers, self.organizations, self.locations,
            self.extra_providers, self.extra_participants,
        ])


def load_from_drive(file_id: str) -> SupplementaryPII:
    """
    Download the CSV from Google Drive and return a SupplementaryPII object.
    Raises on Drive errors so the caller can decide whether to abort or continue.
    """
    creds, _ = google_auth_default(
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    try:
        response = svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    except HttpError as exc:
        raise RuntimeError(f"Could not download PII CSV (file_id={file_id}): {exc}") from exc

    content = response.decode("utf-8") if isinstance(response, bytes) else response
    return _parse_csv(content)


def load_from_config(config) -> SupplementaryPII:
    """
    Convenience wrapper: reads PII_CSV_FILE_ID from env/config and loads the CSV.
    Returns an empty SupplementaryPII if the env var is not set (non-fatal).
    """
    file_id = os.environ.get("PII_CSV_FILE_ID", "")
    if not file_id:
        logger.info("PII_CSV_FILE_ID not set — skipping supplementary PII CSV")
        return SupplementaryPII()

    try:
        pii = load_from_drive(file_id)
        logger.info(
            "Supplementary PII loaded from CSV: %d carers, %d orgs, %d locations, "
            "%d extra providers, %d extra participants",
            len(pii.carers), len(pii.organizations), len(pii.locations),
            len(pii.extra_providers), len(pii.extra_participants),
        )
        return pii
    except Exception as exc:
        logger.error("Failed to load supplementary PII CSV: %s", exc)
        # Non-fatal — pipeline continues without the supplementary data
        return SupplementaryPII()


def _parse_csv(content: str) -> SupplementaryPII:
    pii = SupplementaryPII()
    reader = csv.DictReader(io.StringIO(content))

    if reader.fieldnames is None or not {"Type", "Name"}.issubset(
        {f.strip() for f in reader.fieldnames}
    ):
        logger.warning("PII CSV missing required 'Type' and 'Name' columns — skipping")
        return pii

    for row in reader:
        pii_type = row.get("Type", "").strip()
        name = row.get("Name", "").strip()
        if not name:
            continue

        if pii_type == "Carer":
            pii.carers.add(name)
            pii.carers.update(generate_name_variations(name))
        elif pii_type == "Organization":
            pii.organizations.add(name)
        elif pii_type == "Location":
            pii.locations.add(name)
        elif pii_type == "Provider":
            pii.extra_providers.add(name)
            pii.extra_providers.update(generate_name_variations(name))
        elif pii_type == "Participant":
            pii.extra_participants.add(name)
            pii.extra_participants.update(generate_name_variations(name))

    return pii
