"""
Google Sheets reference code map.

Sheet layout (two tabs in the same spreadsheet):

  "Reference Codes" tab
    A: Reference Code  (PART-001, PART-002, …)
    B: ShiftCare Client ID
    C: Full Name
    D: NDIS Number
    E: Date of Birth
    F: Address
    G: Date First Assigned (ISO)

  "Processed Notes" tab  — idempotency log
    A: Note ID
    B: Processed At (ISO datetime)
    C: Drive File ID
    D: Status  (success | quarantine | error)
    E: Reference Code
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from src.auth import get_credentials

logger = logging.getLogger(__name__)

_REF_SHEET = "Reference Codes"
_LOG_SHEET = "Processed Notes"
_REF_HEADER = ["Reference Code", "ShiftCare Client ID", "Full Name",
               "NDIS Number", "Date of Birth", "Address", "Date First Assigned"]
_LOG_HEADER = ["Note ID", "Processed At", "Drive File ID", "Status", "Reference Code"]


class ReferenceMap:
    """
    In-memory reference map backed by a Google Sheet.

    Call load() at the start of each run, then get_or_create_code() for each
    participant, then save() once at the end.  Call log_processed_note() after
    each note succeeds or is quarantined.
    """

    def __init__(self, config) -> None:
        self._sheet_id = config.reference_map_sheet_id
        creds = get_credentials(
            client_secrets_path=config.oauth_client_secrets_path,
            token_path=config.oauth_token_path,
        )
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

        # {shiftcare_client_id (str): participant_dict}
        self._by_client_id: dict[str, dict] = {}
        # {note_id (str): True}
        self._processed_note_ids: set[str] = set()
        # rows that need to be appended at save() time
        self._new_ref_rows: list[list] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load both tabs from the spreadsheet into memory."""
        self._ensure_tabs_exist()
        self._load_reference_codes()
        self._load_processed_notes()
        logger.info(
            "Reference map loaded: %d participants, %d processed notes",
            len(self._by_client_id),
            len(self._processed_note_ids),
        )

    def save(self) -> None:
        """Append any new reference-code rows accumulated during this run."""
        if not self._new_ref_rows:
            return
        self._append_rows(_REF_SHEET, self._new_ref_rows)
        logger.info("Saved %d new reference code row(s)", len(self._new_ref_rows))
        self._new_ref_rows.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create_code(self, client_id: int | str, client_data: dict) -> str:
        """
        Return the reference code for a participant, assigning a new PART-NNN
        code if this is the first time we have seen them.
        """
        cid = str(client_id)
        if cid in self._by_client_id:
            return self._by_client_id[cid]["ref_code"]

        # Assign next code
        existing_count = len(self._by_client_id)
        code = f"PART-{existing_count + 1:03d}"

        full_name = (
            f"{client_data.get('first_name', '')} {client_data.get('last_name', '')}".strip()
        )
        entry = {
            "ref_code": code,
            "client_id": cid,
            "full_name": full_name,
            "ndis_number": client_data.get("ndis_number", ""),
            "date_of_birth": client_data.get("date_of_birth", ""),
            "address": client_data.get("address", ""),
            "first_name": client_data.get("first_name", ""),
            "last_name": client_data.get("last_name", ""),
            "phone": client_data.get("phone", ""),
            "email": client_data.get("email", ""),
            "date_first_assigned": datetime.now(timezone.utc).isoformat(),
        }
        self._by_client_id[cid] = entry
        self._new_ref_rows.append([
            code, cid, full_name,
            entry["ndis_number"],
            entry["date_of_birth"],
            entry["address"],
            entry["date_first_assigned"],
        ])
        logger.info("Assigned new reference code %s to client_id=%s (%s)", code, cid, full_name)
        return code

    def get_all_participants(self) -> list[dict]:
        """Return all known participant dicts (for de-identification context)."""
        return list(self._by_client_id.values())

    def is_note_processed(self, note_id: int | str) -> bool:
        return str(note_id) in self._processed_note_ids

    def log_processed_note(
        self,
        note_id: int | str,
        status: str,
        drive_file_id: str,
        ref_code: str = "",
    ) -> None:
        """Append a single row to the Processed Notes tab immediately."""
        nid = str(note_id)
        row = [
            nid,
            datetime.now(timezone.utc).isoformat(),
            drive_file_id,
            status,
            ref_code,
        ]
        self._append_rows(_LOG_SHEET, [row])
        self._processed_note_ids.add(nid)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_reference_codes(self) -> None:
        values = self._read_sheet(f"{_REF_SHEET}!A2:G")
        for row in values:
            if len(row) < 2:
                continue
            code = row[0].strip()
            cid = str(row[1]).strip()
            if not code or not cid:
                continue
            self._by_client_id[cid] = {
                "ref_code": code,
                "client_id": cid,
                "full_name": row[2] if len(row) > 2 else "",
                "ndis_number": row[3] if len(row) > 3 else "",
                "date_of_birth": row[4] if len(row) > 4 else "",
                "address": row[5] if len(row) > 5 else "",
                # Split name for deidentifier
                "first_name": (row[2].split()[0] if len(row) > 2 and row[2] else ""),
                "last_name": (row[2].split()[-1] if len(row) > 2 and len(row[2].split()) > 1 else ""),
                "phone": "",
                "email": "",
            }

    def _load_processed_notes(self) -> None:
        values = self._read_sheet(f"{_LOG_SHEET}!A2:A")
        for row in values:
            if row:
                self._processed_note_ids.add(str(row[0]).strip())

    def _ensure_tabs_exist(self) -> None:
        """Create the two required tabs if they don't exist yet."""
        meta = (
            self._svc.spreadsheets()
            .get(spreadsheetId=self._sheet_id)
            .execute()
        )
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        requests_body = []
        for title in (_REF_SHEET, _LOG_SHEET):
            if title not in existing:
                requests_body.append({"addSheet": {"properties": {"title": title}}})

        if requests_body:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={"requests": requests_body},
            ).execute()
            # Write headers
            if _REF_SHEET not in existing:
                self._append_rows(_REF_SHEET, [_REF_HEADER])
            if _LOG_SHEET not in existing:
                self._append_rows(_LOG_SHEET, [_LOG_HEADER])

    def _read_sheet(self, range_: str) -> list[list]:
        try:
            resp = (
                self._svc.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=range_)
                .execute()
            )
            return resp.get("values", [])
        except HttpError as exc:
            logger.error("Sheets read error (%s): %s", range_, exc)
            return []

    def _append_rows(self, tab: str, rows: list[list]) -> None:
        try:
            self._svc.spreadsheets().values().append(
                spreadsheetId=self._sheet_id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()
        except HttpError as exc:
            logger.error("Sheets append error (%s): %s", tab, exc)
            raise
