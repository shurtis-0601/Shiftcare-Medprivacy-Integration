"""
Local / Windows Task Scheduler runner for the ShiftCare → MedPrivacy pipeline.

Orchestrates:
  1. Browser scrape of ShiftCare (unless --skip-scrape)
  2. CSV ingestion
  3. De-identification via main._run()
  4. CSV archival to processed/
  5. Run log (local CSV + Drive upload)

Run manually:
    python run_local.py
    python run_local.py --date 2025-05-16
    python run_local.py --skip-scrape   # use an existing CSV in input/
"""

# ---------------------------------------------------------------------------
# Load .env FIRST — before any module that reads env vars
# ---------------------------------------------------------------------------

import os
import sys
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    env_file = Path(path)
    if not env_file.exists():
        print(
            f"[WARNING] .env file not found at {env_file.resolve()} — "
            "relying on system env vars"
        )
        return
    with open(env_file) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# ---------------------------------------------------------------------------
# Logging (dual: stdout + file)
# ---------------------------------------------------------------------------

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Now safe to import pipeline modules
# ---------------------------------------------------------------------------

import argparse
import shutil
from datetime import date, datetime, timezone

import pytz

import main as pipeline_main
from src.config import Config
from src.csv_ingestor import load_from_csv, count_rows
from src.notifier import Notifier
from src.run_logger import RunLogger
from src.shiftcare_scraper import run_scraper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_target_date(raw_date: date | None, config: Config) -> date:
    if raw_date is not None:
        return raw_date
    tz = pytz.timezone(config.timezone)
    return (datetime.now(tz)).date() - __import__("datetime").timedelta(days=1)


def _find_existing_csv(input_dir: Path, target_date: date) -> Path | None:
    """Return path to a CSV for target_date if it already exists in input_dir."""
    candidate = input_dir / f"service_notes_{target_date.isoformat()}.csv"
    return candidate if candidate.exists() else None


def _find_processed_csv(processed_dir: Path, target_date: date) -> Path | None:
    candidate = processed_dir / f"service_notes_{target_date.isoformat()}.csv"
    return candidate if candidate.exists() else None


def _archive_csv(csv_path: Path, processed_dir: Path) -> Path:
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / csv_path.name
    shutil.move(str(csv_path), str(dest))
    logger.info("CSV archived to %s", dest)
    return dest


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ShiftCare → MedPrivacy → Google Drive pipeline (local runner)"
    )
    parser.add_argument(
        "--date",
        help="Target date YYYY-MM-DD (default: yesterday in Melbourne timezone)",
        type=date.fromisoformat,
        default=None,
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip the Playwright scrape step (use an existing CSV in input/)",
    )
    args = parser.parse_args()

    run_at = datetime.now(timezone.utc)

    # ---- Config ----
    config = Config()
    target_date = _resolve_target_date(args.date, config)
    logger.info("Pipeline starting for %s", target_date)

    input_dir = Path(os.environ.get("PIPELINE_INPUT_DIR", "./input"))
    processed_dir = Path(os.environ.get("PIPELINE_PROCESSED_DIR", "./processed"))
    log_dir = Path(os.environ.get("PIPELINE_LOG_DIR", "./logs"))

    run_logger = RunLogger(log_dir, drive_audit_folder_id=os.environ.get("DRIVE_AUDIT_FOLDER_ID"))
    notifier = Notifier(config)

    # ---- Idempotency: already fully processed? ----
    if _find_processed_csv(processed_dir, target_date) is not None:
        logger.info(
            "CSV for %s already in processed/ folder — pipeline already ran for this date. "
            "Exiting with 0 (note-level idempotency is also enforced by ReferenceMap).",
            target_date,
        )
        sys.exit(0)

    # ---- Step 1: Scrape ----
    csv_path: Path | None = None

    if args.skip_scrape:
        logger.info("--skip-scrape: looking for existing CSV in %s", input_dir)
        csv_path = _find_existing_csv(input_dir, target_date)
        if csv_path is None:
            logger.error(
                "No CSV found for %s in %s. "
                "Either run without --skip-scrape, or manually place the CSV there.",
                target_date,
                input_dir,
            )
            sys.exit(1)
    else:
        sc_email = os.environ.get("SHIFTCARE_EMAIL", "")
        sc_password = os.environ.get("SHIFTCARE_PASSWORD", "")
        if not sc_email or not sc_password:
            logger.error(
                "SHIFTCARE_EMAIL and SHIFTCARE_PASSWORD must be set in .env "
                "to run the browser scrape. Use --skip-scrape if you have a CSV already."
            )
            sys.exit(1)

        # Returns None if CSV already exists (idempotency) — treat as already scraped
        try:
            result = run_scraper(sc_email, sc_password, input_dir, target_date)
        except RuntimeError as exc:
            logger.error("Scraper failed: %s", exc)
            try:
                notifier.send_pipeline_report(
                    target_date,
                    {"total": 0, "uploaded": 0, "quarantined": 0, "skipped": 0, "errors": 1},
                    [],
                    [{"note_id": "scraper", "error": str(exc)}],
                )
            except Exception:  # pylint: disable=broad-except
                pass
            run_logger.record_run(
                run_date=target_date,
                run_at=run_at,
                notes_exported=0,
                notes_processed=0,
                notes_quarantined=0,
                notes_skipped=0,
                errors=[str(exc)],
                csv_path="",
            )
            run_logger.upload_log(config)
            sys.exit(1)

        if result is None:
            # Scraper reported file already existed
            csv_path = _find_existing_csv(input_dir, target_date)
        else:
            csv_path = result

        if csv_path is None:
            logger.error("Scraper returned no CSV path and no existing file found for %s.", target_date)
            sys.exit(1)

    notes_exported = count_rows(csv_path)
    logger.info("CSV contains %d non-empty note rows: %s", notes_exported, csv_path)

    # ---- Step 2: Ingest CSV ----
    clients, staff, notes = load_from_csv(csv_path, target_date)

    # ---- Step 3: Run de-identification pipeline ----
    logger.info("Starting de-identification for %d notes", len(notes))
    stats = pipeline_main._run(
        config,
        target_date,
        clients=clients,
        staff=staff,
        notes=notes,
    )

    # ---- Step 4: Archive CSV ----
    _archive_csv(csv_path, processed_dir)

    # ---- Step 5: Log run ----
    error_list = [f"note_id={e.get('note_id')}: {e.get('error')}" for e in []]
    # errors from _run aren't directly returned; use stats["errors"] count
    error_count = stats.get("errors", 0)
    error_msgs = [f"{error_count} note(s) errored — check pipeline.log"] if error_count else []

    run_logger.record_run(
        run_date=target_date,
        run_at=run_at,
        notes_exported=notes_exported,
        notes_processed=stats.get("uploaded", 0),
        notes_quarantined=stats.get("quarantined", 0),
        notes_skipped=stats.get("skipped", 0),
        errors=error_msgs,
        csv_path=str(csv_path.name),
    )
    run_logger.upload_log(config)

    # ---- Step 6: Summary ----
    print("\n" + "=" * 55)
    print(f"Pipeline complete for {target_date}")
    print(f"  Exported from ShiftCare : {notes_exported}")
    print(f"  Uploaded to Pending     : {stats.get('uploaded', 0)}")
    print(f"  Quarantined             : {stats.get('quarantined', 0)}")
    print(f"  Skipped (dup/empty)     : {stats.get('skipped', 0)}")
    print(f"  Errors                  : {error_count}")
    print("=" * 55)

    if error_count > 0:
        logger.error("Pipeline finished with %d error(s) — review pipeline.log", error_count)
        sys.exit(1)


if __name__ == "__main__":
    main()
