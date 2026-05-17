"""
Reads a ShiftCare case-notes CSV export and returns data in the same shape as
ShiftCareClient.get_clients() / get_employees() / get_service_notes().

Column names are configurable via environment variables to handle ShiftCare
export format changes without code changes.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column name resolution
# ---------------------------------------------------------------------------

def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


# Primary column names (from env with fallback defaults)
_COL_DATE = _env("SC_COL_DATE", "Date")
_COL_FIRST_NAME = _env("SC_COL_FIRST_NAME", "First Name")
_COL_LAST_NAME = _env("SC_COL_LAST_NAME", "Last Name")
_COL_FULL_NAME = _env("SC_COL_FULL_NAME", "Client Name")
_COL_NOTE = _env("SC_COL_NOTE", "Note")
_COL_STAFF = _env("SC_COL_STAFF", "Support Worker")
_COL_NDIS = _env("SC_COL_NDIS", "NDIS Number")
_COL_DOB = _env("SC_COL_DOB", "Date of Birth")
_COL_SERVICE_TYPE = _env("SC_COL_SERVICE_TYPE", "Service Type")

# Fallback aliases tried when the primary column is not found
_ALIASES: dict[str, list[str]] = {
    "date": [_COL_DATE, "Service Date", "Start Date"],
    "first_name": [_COL_FIRST_NAME, "Client First Name", "Participant First Name"],
    "last_name": [_COL_LAST_NAME, "Client Last Name", "Participant Last Name"],
    "full_name": [_COL_FULL_NAME, "Client Name"],
    "note": [_COL_NOTE, "Progress Note", "Case Note", "Description", "Notes"],
    "staff": [_COL_STAFF, "Created By", "Staff", "Worker"],
    "ndis": [_COL_NDIS, "NDIS No.", "NDIS"],
    "dob": [_COL_DOB, "DOB"],
    "service_type": [_COL_SERVICE_TYPE],
}


def _resolve_col(headers: list[str], field: str) -> str | None:
    """Return the first alias for *field* that appears in *headers*, or None."""
    lower_headers = {h.lower(): h for h in headers}
    for alias in _ALIASES.get(field, []):
        if alias in headers:
            return alias
        if alias.lower() in lower_headers:
            return lower_headers[alias.lower()]
    return None


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _client_id(full_name: str) -> str:
    return f"csv_{hashlib.md5(full_name.lower().encode()).hexdigest()[:8]}"


def _note_id(client_key: str, date_str: str, note_text: str) -> str:
    payload = f"{client_key}|{date_str}|{note_text[:100]}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_DATE_FMTS = ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%-d/%-m/%Y", "%m/%d/%Y")


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    for fmt in _DATE_FMTS:
        try:
            return date.fromisoformat(raw) if fmt == "%Y-%m-%d" else _strptime_date(raw, fmt)
        except ValueError:
            continue
    return None


def _strptime_date(raw: str, fmt: str) -> date:
    from datetime import datetime as _dt
    return _dt.strptime(raw, fmt).date()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_from_csv(
    csv_path: Path,
    target_date: date,
) -> tuple[dict, list[dict], list[dict]]:
    """
    Parse a ShiftCare service-notes CSV export.

    Returns:
        clients_dict  — {client_id: client_record} matching ShiftCareClient.get_clients()
        staff_list    — [{"first_name": str, "last_name": str}] deduplicated
        notes_list    — [{"id": str, "client_id": str, "note": str, "created_at": str}]
                        filtered to rows whose date matches target_date
    """
    csv_path = Path(csv_path)
    clients: dict[str, dict] = {}
    notes: list[dict] = []
    staff_seen: dict[str, dict] = {}  # keyed by full name (lowercased)

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []

        # Resolve column names once
        col_date = _resolve_col(headers, "date")
        col_first = _resolve_col(headers, "first_name")
        col_last = _resolve_col(headers, "last_name")
        col_full = _resolve_col(headers, "full_name")
        col_note = _resolve_col(headers, "note")
        col_staff = _resolve_col(headers, "staff")
        col_ndis = _resolve_col(headers, "ndis")
        col_dob = _resolve_col(headers, "dob")

        logger.debug(
            "CSV column mapping — date=%s first=%s last=%s full=%s note=%s staff=%s ndis=%s dob=%s",
            col_date, col_first, col_last, col_full, col_note, col_staff, col_ndis, col_dob,
        )

        matched_rows = 0
        for row in reader:
            # ---- Date filtering ----
            raw_date = row.get(col_date, "").strip() if col_date else ""
            if raw_date:
                row_date = _parse_date(raw_date)
                if row_date != target_date:
                    continue
            # If there is no date column at all we include every row

            matched_rows += 1

            # ---- Note text ----
            note_text = row.get(col_note, "").strip() if col_note else ""
            if not note_text:
                continue

            # ---- Client name ----
            first_name = row.get(col_first, "").strip() if col_first else ""
            last_name = row.get(col_last, "").strip() if col_last else ""

            if not first_name and not last_name and col_full:
                full_raw = row.get(col_full, "").strip()
                parts = full_raw.split(None, 1)
                first_name = parts[0] if parts else ""
                last_name = parts[1] if len(parts) > 1 else ""

            full_name = f"{first_name} {last_name}".strip() or "Unknown"
            client_id = _client_id(full_name)

            # ---- Build / update client record ----
            if client_id not in clients:
                ndis = row.get(col_ndis, "").strip() if col_ndis else ""
                dob = row.get(col_dob, "").strip() if col_dob else ""
                clients[client_id] = {
                    "id": client_id,
                    "first_name": first_name,
                    "last_name": last_name,
                    "ndis_number": ndis,
                    "date_of_birth": dob,
                    "address": "",
                    "phone": "",
                    "email": "",
                }

            # ---- Staff ----
            if col_staff:
                staff_raw = row.get(col_staff, "").strip()
                if staff_raw:
                    key = staff_raw.lower()
                    if key not in staff_seen:
                        parts = staff_raw.split(None, 1)
                        staff_seen[key] = {
                            "first_name": parts[0],
                            "last_name": parts[1] if len(parts) > 1 else "",
                        }

            # ---- Note record ----
            date_str = target_date.isoformat()
            note_id = _note_id(client_id, date_str, note_text)
            notes.append({
                "id": note_id,
                "client_id": client_id,
                "note": note_text,
                "created_at": date_str,
            })

    if matched_rows == 0:
        logger.warning(
            "No rows matched target_date=%s in %s. "
            "Check that the CSV date column ('%s') uses DD/MM/YYYY or YYYY-MM-DD format.",
            target_date,
            csv_path.name,
            col_date or "not found",
        )

    logger.info(
        "CSV ingest complete: %d clients, %d staff, %d notes (date=%s)",
        len(clients),
        len(staff_seen),
        len(notes),
        target_date,
    )
    return clients, list(staff_seen.values()), notes


def count_rows(csv_path: Path | str) -> int:
    """Count non-empty note rows in a ShiftCare CSV (does not filter by date)."""
    csv_path = Path(csv_path)
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        col_note = _resolve_col(headers, "note")
        if col_note is None:
            return sum(1 for _ in reader)
        return sum(1 for row in reader if row.get(col_note, "").strip())
