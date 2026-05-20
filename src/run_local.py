"""
Local / Windows Task Scheduler runner for the ShiftCare → MedPrivacy pipeline.

Orchestrates:
  1. Browser scrape of ShiftCare Events page — one PDF per participant
     (unless --skip-scrape)
  2. De-identification via main._run()  (reads PDFs from input/)
  3. PDF archival to processed/
  4. Run log (local CSV + optional Drive upload)

Run manually:
    python src/run_local.py
    python src/run_local.py --date 2025-05-16
    python src/run_local.py --skip-scrape   # process PDFs already in input/
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
from src.notifier import Notifier
from src.reference_map import ReferenceMap
from src.run_logger import RunLogger
from src.shiftcare_scraper import run_scraper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_target_date(raw_date: date | None, config: Config) -> date:
    if raw_date is not None:
        return raw_date
    tz = pytz.timezone(config.timezone)
    import datetime as _dt
    return (datetime.now(tz)).date() - _dt.timedelta(days=1)


def _find_processed_pdfs(processed_dir: Path, target_date: date) -> list[Path]:
    """Return any PDFs for target_date already in the processed/ folder."""
    return list(processed_dir.glob(f"{target_date.isoformat()}-PART-*.pdf"))


def _find_existing_pdfs(input_dir: Path, target_date: date) -> list[Path]:
    """Return PDFs for target_date already in the input/ folder."""
    return list(input_dir.glob(f"{target_date.isoformat()}-PART-*.pdf"))


def _archive_pdfs(pdf_paths: list[Path], processed_dir: Path) -> None:
    processed_dir.mkdir(parents=True, exist_ok=True)
    for pdf in pdf_paths:
        dest = processed_dir / pdf.name
        shutil.move(str(pdf), str(dest))
    logger.info("Archived %d PDF(s) to %s", len(pdf_paths), processed_dir)


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
        help="Skip the Playwright scrape step (use PDFs already in input/)",
    )
    args = parser.parse_args()

    run_at = datetime.now(timezone.utc)

    # ---- Config ----
    config = Config()
    target_date = _resolve_target_date(args.date, config)
    logger.info("Pipeline starting for %s", target_date)

    input_dir = Path(os.environ.get("PIPELINE_INPUT_DIR", config.input_folder))
    processed_dir = Path(os.environ.get("PIPELINE_PROCESSED_DIR", "./processed"))
    log_dir = Path(os.environ.get("PIPELINE_LOG_DIR", "./logs"))

    run_logger = RunLogger(log_dir, drive_audit_folder_id=os.environ.get("DRIVE_AUDIT_FOLDER_ID"))
    notifier = Notifier(config)

    # ---- Idempotency: already fully processed? ----
    if _find_processed_pdfs(processed_dir, target_date):
        logger.info(
            "PDFs for %s already in processed/ — pipeline already ran for this date. "
            "Exiting with 0 (note-level idempotency also enforced by ReferenceMap).",
            target_date,
        )
        sys.exit(0)

    # ---- Step 1: Load reference map (needed by scraper to assign PART-XXX codes) ----
    ref_map = ReferenceMap(config)
    ref_map.load()

    # ---- Step 2: Scrape ----
    pdf_paths: list[Path] = []

    if args.skip_scrape:
        logger.info("--skip-scrape: looking for existing PDFs in %s", input_dir)
        pdf_paths = _find_existing_pdfs(input_dir, target_date)
        if not pdf_paths:
            logger.error(
                "No PDFs found for %s in %s. "
                "Either run without --skip-scrape, or place the PDFs there manually.",
                target_date, input_dir,
            )
            sys.exit(1)
        logger.info("Found %d existing PDF(s) — skipping scrape", len(pdf_paths))
    else:
        sc_email = os.environ.get("SHIFTCARE_EMAIL", "")
        sc_password = os.environ.get("SHIFTCARE_PASSWORD", "")
        if not sc_email or not sc_password:
            logger.error(
                "SHIFTCARE_EMAIL and SHIFTCARE_PASSWORD must be set in .env "
                "to run the browser scrape. Use --skip-scrape if you have PDFs already."
            )
            sys.exit(1)

        try:
            pdf_paths = run_scraper(sc_email, sc_password, input_dir, target_date, ref_map)
        except RuntimeError as exc:
            logger.warning(
                "Scraper failed — will continue with any PDFs already in %s. Error: %s",
                input_dir, exc,
            )
            pdf_paths = _find_existing_pdfs(input_dir, target_date)
            if pdf_paths:
                logger.info(
                    "Found %d PDF(s) already in %s — proceeding with those",
                    len(pdf_paths), input_dir,
                )
            else:
                logger.warning(
                    "No PDFs in %s either — nothing to process for %s",
                    input_dir, target_date,
                )

    if not pdf_paths:
        logger.warning("No PDFs available for %s — exiting.", target_date)
        sys.exit(0)

    # Save any new PART-XXX assignments made during the scrape so that main._run()
    # can see them when it creates a fresh ReferenceMap and loads from the sheet.
    ref_map.save()

    logger.info("%d PDF(s) ready for processing", len(pdf_paths))

    # ---- Step 3: De-identify and upload to Drive ----
    # main._run() creates its own ReferenceMap, loads from the sheet (which now
    # includes the codes saved above), processes each PDF, and uploads to Drive.
    logger.info("Starting de-identification pipeline")
    stats = pipeline_main._run(config, target_date)

    # ---- Step 4: Archive PDFs ----
    _archive_pdfs(pdf_paths, processed_dir)

    # ---- Step 5: Log run ----
    error_count = stats.get("errors", 0)
    error_msgs = [f"{error_count} note(s) errored — check pipeline.log"] if error_count else []

    run_logger.record_run(
        run_date=target_date,
        run_at=run_at,
        notes_exported=len(pdf_paths),
        notes_processed=stats.get("uploaded", 0),
        notes_quarantined=stats.get("quarantined", 0),
        notes_skipped=stats.get("skipped", 0),
        errors=error_msgs,
        csv_path=f"{len(pdf_paths)} PDF(s)",
    )
    run_logger.upload_log(config)

    # ---- Step 6: Summary ----
    print("\n" + "=" * 55)
    print(f"Pipeline complete for {target_date}")
    print(f"  PDFs scraped            : {len(pdf_paths)}")
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
