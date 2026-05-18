"""
ShiftCare → MedPrivacy → Google Drive pipeline.

Entry points:
  run_pipeline_scheduled(cloud_event)  — triggered by Cloud Scheduler via Pub/Sub
  run_pipeline_http(request)           — HTTP trigger for manual runs / local test calls

The pipeline:
  1. Determines yesterday's date in the Melbourne timezone.
  2. Fetches all active ShiftCare participants (clients) and staff.
  3. Fetches all service/case notes for that date.
  4. For each note:
       a. Looks up (or creates) the participant's PART-NNN reference code.
       b. Runs the MedPrivacy de-identification engine — substitutes all known
          PII with reference codes / redaction tags, then verifies the output
          is clean before writing it anywhere.
       c. Uploads the clean note to Google Drive (Pending folder) or, if
          de-identification verification fails, to the Quarantine folder.
       d. Logs the result to the Processed Notes sheet (idempotency).
  5. Saves any new reference-code assignments back to the Reference Codes sheet.
  6. Sends an email notification if any notes were quarantined or errored.
"""

import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import functions_framework
import pytz

from src.config import Config
from src.csv_ingestor import load_from_csv
from src.deidentifier import MedPrivacyDeidentifier
from src.drive_uploader import DriveUploader
from src.notifier import Notifier
from src.pii_csv_loader import load_from_config as load_supplementary_pii
from src.reference_map import ReferenceMap

# Cloud Logging picks up the standard logging module automatically when running
# inside a Cloud Function.  For local runs it falls back to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cloud Function entry points
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def run_pipeline_scheduled(cloud_event):
    """Pub/Sub Cloud Event — invoked by Cloud Scheduler."""
    config = Config()
    _run(config)


@functions_framework.http
def run_pipeline_http(request):
    """HTTP trigger — for manual runs and local testing."""
    config = Config()

    # Allow overriding the target date via ?date=YYYY-MM-DD for back-fills
    raw_date = request.args.get("date") if request.args else None
    target_date = None
    if raw_date:
        try:
            target_date = date.fromisoformat(raw_date)
        except ValueError:
            return f"Invalid date format: {raw_date!r}. Use YYYY-MM-DD.", 400

    stats = _run(config, target_date=target_date)
    return {
        "status": "ok",
        "stats": stats,
        "date": str(target_date or _yesterday(config.timezone)),
    }, 200


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def _run(config: Config, target_date: date | None = None) -> dict:
    if target_date is None:
        target_date = _yesterday(config.timezone)

    logger.info("Pipeline starting for date: %s", target_date)

    ref_map = ReferenceMap(config)
    deidentifier = MedPrivacyDeidentifier()
    uploader = DriveUploader(config)
    notifier = Notifier(config)

    # ---- Load reference map (participants seen so far + processed note log) ----
    ref_map.load()

    # ---- Load supplementary PII from master CSV in Google Drive ----
    supplementary_pii = load_supplementary_pii(config)

    # ---- Read notes from CSV files placed in the input folder by the scraper ----
    input_folder = Path(config.input_folder)
    csv_files = sorted(input_folder.glob("*.csv"))
    if not csv_files:
        logger.warning("No CSV files found in %s — nothing to process", input_folder)
        return {"total": 0}

    clients: dict = {}
    _staff_seen: dict[str, dict] = {}
    notes: list[dict] = []
    for csv_path in csv_files:
        c, s, n = load_from_csv(csv_path, target_date)
        clients.update(c)
        notes.extend(n)
        for member in s:
            key = f"{member['first_name']} {member['last_name']}".lower()
            _staff_seen[key] = member
    staff: list[dict] = list(_staff_seen.values())

    stats: dict = defaultdict(int)
    stats["total"] = len(notes)
    quarantined: list[dict] = []
    errors: list[dict] = []

    # ---- Process each note ----
    for note in notes:
        note_id = note.get("id")
        client_id = note.get("client_id")
        note_text = (note.get("note") or "").strip()

        if not note_text:
            stats["skipped"] += 1
            logger.info("Note %s has no text body — skipping", note_id)
            continue

        # Idempotency: skip if already processed in a previous run
        if ref_map.is_note_processed(note_id):
            stats["skipped"] += 1
            logger.info("Note %s already processed — skipping", note_id)
            continue

        try:
            client_data = clients.get(client_id, {})

            # Assign / retrieve reference code for this participant
            ref_code = ref_map.get_or_create_code(client_id, client_data)

            # Build full participant context for the deidentifier
            # (all known participants so cross-mentions in notes are also redacted)
            all_participants = ref_map.get_all_participants()

            # De-identify
            result = deidentifier.deidentify(
                text=note_text,
                participants=all_participants,
                staff=staff,
                supplementary_pii=supplementary_pii,
            )

            created_at = note.get("created_at", str(target_date))[:10]  # YYYY-MM-DD
            filename = f"{created_at}_{ref_code}_note_{note_id}"

            if result.is_quarantined:
                filename += "_QUARANTINE.txt"
                file_id = uploader.upload_to_quarantine(
                    _format_quarantine_file(note_id, ref_code, result),
                    filename,
                )
                ref_map.log_processed_note(note_id, "quarantine", file_id, ref_code)
                quarantined.append({
                    "note_id": note_id,
                    "ref_code": ref_code,
                    "reason": result.quarantine_reason,
                })
                stats["quarantined"] += 1
                logger.warning(
                    "Note %s quarantined (ref %s): %s",
                    note_id, ref_code, result.quarantine_reason,
                )
            else:
                filename += ".txt"
                file_id = uploader.upload_to_pending(
                    _format_output_file(target_date, ref_code, result),
                    filename,
                )
                ref_map.log_processed_note(note_id, "success", file_id, ref_code)
                stats["uploaded"] += 1
                logger.info(
                    "Note %s → %s (substitutions: %s)",
                    note_id, filename, result.substitutions,
                )

        except Exception as exc:  # pylint: disable=broad-except
            stats["errors"] += 1
            errors.append({"note_id": note_id, "error": str(exc)})
            logger.exception("Unhandled error processing note %s", note_id)

    # ---- Persist new reference-code assignments ----
    ref_map.save()

    # ---- Notify if anything needs human attention ----
    if quarantined or errors:
        notifier.send_pipeline_report(target_date, dict(stats), quarantined, errors)

    logger.info(
        "Pipeline complete for %s — %s",
        target_date,
        dict(stats),
    )
    return dict(stats)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yesterday(timezone_name: str) -> date:
    tz = pytz.timezone(timezone_name)
    return (datetime.now(tz) - timedelta(days=1)).date()


def _format_output_file(target_date: date, ref_code: str, result) -> str:
    lines = [
        f"Reference: {ref_code}",
        f"Date: {target_date}",
        f"Substitutions: {result.substitutions}",
        "",
        "--- DE-IDENTIFIED NOTE ---",
        "",
        result.deidentified_text,
    ]
    return "\n".join(lines)


def _format_quarantine_file(note_id, ref_code: str, result) -> str:
    lines = [
        "*** QUARANTINED — DO NOT DISTRIBUTE ***",
        f"Note ID: {note_id}",
        f"Reference: {ref_code}",
        f"Quarantine reason: {result.quarantine_reason}",
        "",
        "--- DE-IDENTIFIED TEXT (INCOMPLETE) ---",
        "",
        result.deidentified_text,
    ]
    return "\n".join(lines)
