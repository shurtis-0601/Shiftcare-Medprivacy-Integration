# ShiftCare → MedPrivacy → Google Drive Pipeline

## Project Overview

This pipeline automates the daily de-identification of NDIS participant case notes for Sinclair's organisation. It logs into ShiftCare using browser automation, exports the previous day's service notes as a CSV, runs each note through the MedPrivacy de-identification engine (which replaces participant names, NDIS numbers, dates of birth, phone numbers, email addresses, and other PII with reference codes or redaction tags), and uploads the clean notes to a Google Drive Pending folder for review. The raw CSV and all intermediate data stay on Sinclair's PC only and must never be shared or committed to version control.

## Architecture

- **Playwright scraper** (`src/shiftcare_scraper.py`) logs into ShiftCare and downloads the case-notes CSV for the target date.
- **CSV ingestor** (`src/csv_ingestor.py`) reads the CSV and converts it into the same data structures as the ShiftCare API client.
- **MedPrivacy de-identification engine** (`src/deidentifier.py`) substitutes all known PII with reference codes and `[REDACTED]` tags, then re-scans the output to verify no PII remains before writing anything.
- **Drive uploader** (`src/drive_uploader.py`) writes clean notes to the Google Drive **Pending** folder, or quarantined notes to the **Quarantine** folder if verification fails.
- **Reference map** (`src/reference_map.py`) maintains a `PART-NNN` code for each participant and a processed-notes log in Google Sheets (idempotency).
- **Run logger** (`src/run_logger.py`) appends one row per pipeline run to `logs/pipeline_run_log.csv` and uploads it to the Google Drive **Audit** folder.

## Prerequisites

- Python 3.11 or later
- `pip`
- A GCP project with a service account JSON key (Editor on Drive folders and Sheets)
- Google Drive folders created and shared with the service account:
  - **Pending** — de-identified notes awaiting review
  - **Quarantine** — notes that failed de-identification verification
  - **Pipeline Audit Logs** — run log uploads
- A Google Sheet (separate secure location) shared with the service account as Editor

## Setup

1. **Clone the repository** and enter the directory:
   ```
   git clone <repo-url>
   cd Shiftcare-Medprivacy-Integration
   ```

2. **Create a virtual environment** and activate it:
   ```
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   source .venv/bin/activate     # macOS/Linux
   ```

3. **Install dependencies**:
   ```
   pip install -r requirements.txt
   ```

4. **Install the Playwright browser**:
   ```
   playwright install chromium
   ```

5. **Configure environment variables** — copy the example file and fill in every value:
   ```
   copy .env.example .env        # Windows
   cp .env.example .env          # macOS/Linux
   ```
   Open `.env` in a text editor and set:
   - `GCP_PROJECT_ID` — your GCP project ID
   - `SHIFTCARE_EMAIL` / `SHIFTCARE_PASSWORD` — ShiftCare login credentials
   - `DRIVE_PENDING_FOLDER_ID`, `DRIVE_QUARANTINE_FOLDER_ID`, `DRIVE_AUDIT_FOLDER_ID`
   - `REFERENCE_MAP_SHEET_ID`
   - `NOTIFICATION_EMAIL`
   - `GOOGLE_APPLICATION_CREDENTIALS` — path to service account JSON key

6. **Share Drive folders and the Sheet** with the service account email address (found in the JSON key file under `"client_email"`) as **Editor**.

## Running manually

Run the full pipeline for yesterday (default):
```
python run_local.py
```

Run for a specific date (back-fill):
```
python run_local.py --date 2025-05-16
```

Skip the ShiftCare browser scrape (use an existing CSV in `input/`):
```
python run_local.py --skip-scrape
```

## Running unit tests

No credentials required:
```
python test_local.py
```

## Running integration tests

Requires real GCP credentials and environment variables:
```
set INTEGRATION=1 && python test_local.py       # Windows
INTEGRATION=1 python test_local.py              # macOS/Linux
```

## Windows Task Scheduler setup

1. **Edit `run_pipeline.bat`** in the repo folder — set `PIPELINE_DIR` to the full path of the repo and `PYTHON` to the full path of `.venv\Scripts\python.exe`.

2. Open **Task Scheduler** → **Create Basic Task**.

3. **Name**: `ShiftCare MedPrivacy Pipeline`

4. **Trigger**: Daily — Start: `7:00 AM`, Recur every `1` day.

5. **Action**: Start a program → browse to `run_pipeline.bat`.

6. Click **Finish**, then right-click the task → **Properties** → **General** tab:
   - Check **Run whether user is logged on or not**
   - Check **Run with highest privileges**
   - Configure for: **Windows 10/11**

7. **Settings** tab:
   - If the task fails, restart every **5 minutes**, up to **3 times**.

8. **Melbourne timezone note**: The pipeline uses `TIMEZONE=Australia/Melbourne`. Windows must sync its clock correctly (AEST/AEDT auto-adjusts via Windows Time Service — no manual action needed as long as the PC is set to the correct time zone in Windows Settings → Time & Language).

## What to check each morning

Sinclair's daily checklist:

1. **Check `pipeline.log`** in the repo folder — the last line should say `Pipeline complete`.
2. **Check the Google Drive Pending folder** — new `.txt` files for yesterday's date should be present.
3. **Check the Google Drive Audit folder** — `pipeline_run_log.csv` should show yesterday's run with `0` in the Errors column.
4. **If any quarantined notes**: open the Quarantine Drive folder, review each file manually before moving to Pending.
5. **If `pipeline.log` shows an error**: check the `screenshots/` folder for browser screenshots showing what went wrong. An email notification is also sent to `NOTIFICATION_EMAIL`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Could not find email input" | ShiftCare login page changed | Set `SC_SEL_EMAIL` in `.env` to the new CSS selector |
| "Could not navigate to Reports → Service Notes" | ShiftCare menu changed | Set `SC_EXPORT_URL` in `.env` to the direct export page URL |
| "Could not find export button" | Button text changed | Set `SC_EXPORT_BTN_TEXT` in `.env` to the correct text |
| Task Scheduler shows "Last Run Result: 0x1" | Pipeline had errors | Check `pipeline.log` for details |
| No files in Pending folder | Notes already processed today (idempotency) | Normal — confirm `pipeline_run_log.csv` shows 0 new notes |

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `GCP_PROJECT_ID` | Yes | GCP project ID |
| `SHIFTCARE_EMAIL` | Yes | ShiftCare login email |
| `SHIFTCARE_PASSWORD` | Yes | ShiftCare login password |
| `DRIVE_PENDING_FOLDER_ID` | Yes | Google Drive folder ID for de-identified notes |
| `DRIVE_QUARANTINE_FOLDER_ID` | Yes | Google Drive folder ID for quarantined notes |
| `DRIVE_AUDIT_FOLDER_ID` | Yes | Google Drive folder ID for run logs |
| `REFERENCE_MAP_SHEET_ID` | Yes | Google Sheets spreadsheet ID for reference codes |
| `NOTIFICATION_EMAIL` | Yes | Email address for pipeline error/quarantine alerts |
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes | Path to service account JSON key file |
| `SHIFTCARE_API_KEY` | No | ShiftCare API key (only needed for Cloud Function deployment) |
| `GMAIL_SENDER_EMAIL` | No | Gmail address to send notifications from (requires DWD) |
| `SHIFTCARE_BASE_URL` | No | ShiftCare base URL (default: `https://app.shiftcare.com`) |
| `SHIFTCARE_HEADLESS` | No | Set to `false` to show the browser during scraping (default: `true`) |
| `SC_EXPORT_URL` | No | Direct URL to the export page — set after first manual navigation |
| `SC_NAV_REPORTS_TEXT` | No | Menu item text for Reports (default: `Reports`) |
| `SC_NAV_NOTES_TEXT` | No | Menu item text for Service Notes (default: `Service Notes`) |
| `SC_EXPORT_BTN_TEXT` | No | Export button text (default: `Export`) |
| `SC_NAV_TIMEOUT_MS` | No | Navigation timeout in milliseconds (default: `30000`) |
| `SC_SEL_EMAIL` | No | CSS selector for email input on login page |
| `SC_SEL_PASSWORD` | No | CSS selector for password input on login page |
| `SC_SEL_SUBMIT` | No | CSS selector for login submit button |
| `SCREENSHOT_DIR` | No | Directory for browser screenshots (default: `./screenshots`) |
| `PIPELINE_INPUT_DIR` | No | Directory for downloaded CSVs (default: `./input`) |
| `PIPELINE_PROCESSED_DIR` | No | Directory for archived CSVs (default: `./processed`) |
| `PIPELINE_LOG_DIR` | No | Directory for run logs (default: `./logs`) |
| `TIMEZONE` | No | Timezone for date calculation (default: `Australia/Melbourne`) |
| `PII_CSV_FILE_ID` | No | Drive file ID of supplementary PII CSV |
| `QUARANTINE_FINDING_THRESHOLD` | No | Max DLP findings before quarantining (default: `0`) |

## Security notes

- **`.env` must never be committed to git.** It is listed in `.gitignore` but double-check before pushing.
- The `processed/` and `input/` folders contain identified participant data — keep them on Sinclair's PC only and ensure they are excluded from any cloud backup that is not end-to-end encrypted.
- The service account JSON key file should be stored securely (not in the repo folder) and rotated annually.
- The Google Sheet (Reference Codes + Processed Notes) must be in a separate secure location, not in any shared working folder.
