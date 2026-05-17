"""
Playwright-based browser automation to scrape case-note CSVs from ShiftCare.

All selectors and navigation text are configurable via environment variables so
that minor ShiftCare UI changes can be fixed without touching code.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import date, datetime
from pathlib import Path

import pytz
from playwright.async_api import async_playwright, Page, Download

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-var helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------

async def _screenshot(page: Page, label: str) -> None:
    screenshot_dir = Path(_env("SCREENSHOT_DIR", "./screenshots"))
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = screenshot_dir / f"{ts}_{label}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot saved: %s", path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not save screenshot '%s': %s", label, exc)


# ---------------------------------------------------------------------------
# Core async scraper
# ---------------------------------------------------------------------------

async def scrape_case_notes(
    email: str,
    password: str,
    input_dir: Path,
    target_date: date | None = None,
) -> Path | None:
    """
    Browser-automate a ShiftCare CSV export for the given date.

    Returns the Path to the saved CSV, or None if a CSV for this date already
    exists (idempotency guard).
    """
    if target_date is None:
        tz = pytz.timezone(_env("TIMEZONE", "Australia/Melbourne"))
        target_date = (datetime.now(tz)).date()

    input_dir = Path(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)

    csv_filename = f"service_notes_{target_date.isoformat()}.csv"
    csv_path = input_dir / csv_filename

    if csv_path.exists():
        logger.info("CSV already exists for %s — skipping scrape: %s", target_date, csv_path)
        return None

    base_url = _env("SHIFTCARE_BASE_URL", "https://app.shiftcare.com").rstrip("/")
    headless = _env("SHIFTCARE_HEADLESS", "true").lower() not in ("false", "0", "no")
    timeout_ms = _env_int("SC_NAV_TIMEOUT_MS", 30000)

    # Selectors — overridable so ShiftCare UI changes are fixed via .env
    sel_email = _env("SC_SEL_EMAIL", "input[type='email'], input[name='email'], #user_email")
    sel_password = _env("SC_SEL_PASSWORD", "input[type='password'], input[name='password'], #user_password")
    sel_submit = _env("SC_SEL_SUBMIT", "input[type='submit'], button[type='submit']")
    export_url = _env("SC_EXPORT_URL", "")
    nav_reports_text = _env("SC_NAV_REPORTS_TEXT", "Reports")
    nav_notes_text = _env("SC_NAV_NOTES_TEXT", "Service Notes")
    export_btn_text = _env("SC_EXPORT_BTN_TEXT", "Export")

    # AU date format
    date_str_au = target_date.strftime("%d/%m/%Y")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            # ---- Login ----
            logger.info("Navigating to ShiftCare login")
            await page.goto(f"{base_url}/users/sign_in", timeout=timeout_ms)
            await _screenshot(page, "01_login_page")

            # Fill email
            email_input = page.locator(sel_email).first
            if not await email_input.count():
                raise RuntimeError(
                    "Could not find email input. Check SC_SEL_EMAIL env var. "
                    "See screenshots/ for browser state."
                )
            await email_input.fill(email)

            # Fill password
            password_input = page.locator(sel_password).first
            if not await password_input.count():
                raise RuntimeError(
                    "Could not find password input. Check SC_SEL_PASSWORD env var. "
                    "See screenshots/ for browser state."
                )
            await password_input.fill(password)
            await _screenshot(page, "02_login_filled")

            # Submit
            submit_btn = page.locator(sel_submit).first
            if not await submit_btn.count():
                raise RuntimeError(
                    "Could not find login submit button. Check SC_SEL_SUBMIT env var. "
                    "See screenshots/ for browser state."
                )
            await submit_btn.click()

            # Wait for login to complete (URL moves away from sign_in)
            await page.wait_for_url(
                lambda u: "sign_in" not in u,
                timeout=timeout_ms,
            )
            await _screenshot(page, "03_post_login")
            logger.info("Login successful, current URL: %s", page.url)

            # ---- Navigate to export page ----
            if export_url:
                logger.info("Navigating directly to SC_EXPORT_URL: %s", export_url)
                await page.goto(export_url, timeout=timeout_ms)
            else:
                logger.info("Navigating via menu: %s → %s", nav_reports_text, nav_notes_text)
                reports_link = page.get_by_text(nav_reports_text, exact=False).first
                if not await reports_link.count():
                    raise RuntimeError(
                        f"Could not find '{nav_reports_text}' in navigation. "
                        "Set SC_EXPORT_URL in .env to skip menu navigation, "
                        "or set SC_NAV_REPORTS_TEXT to the correct menu text. "
                        "See screenshots/ for browser state."
                    )
                await reports_link.click()
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                await _screenshot(page, "04_reports_menu")

                notes_link = page.get_by_text(nav_notes_text, exact=False).first
                if not await notes_link.count():
                    raise RuntimeError(
                        f"Could not find '{nav_notes_text}' submenu item. "
                        "Set SC_EXPORT_URL in .env to skip menu navigation, "
                        "or set SC_NAV_NOTES_TEXT to the correct text. "
                        "See screenshots/ for browser state."
                    )
                await notes_link.click()
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)

            await _screenshot(page, "05_notes_page")
            logger.info("On notes/export page: %s", page.url)

            # ---- Set date range ----
            await _set_date_range(page, date_str_au, timeout_ms)
            await _screenshot(page, "06_dates_set")

            # ---- Trigger download ----
            logger.info("Triggering CSV export")
            fallback_selectors = [
                "button:has-text('CSV')",
                "a:has-text('CSV')",
                "a:has-text('Export')",
                "[data-action*='export']",
            ]

            downloaded_path: Path | None = None
            async with page.expect_download(timeout=timeout_ms) as dl_info:
                clicked = False
                # Try the configured export button text first
                export_btn = page.get_by_text(export_btn_text, exact=False).first
                if await export_btn.count():
                    await export_btn.click()
                    clicked = True
                else:
                    for selector in fallback_selectors:
                        btn = page.locator(selector).first
                        if await btn.count():
                            logger.info("Export button '%s' not found; using fallback: %s", export_btn_text, selector)
                            await btn.click()
                            clicked = True
                            break

                if not clicked:
                    raise RuntimeError(
                        f"Could not find export button (tried text='{export_btn_text}' and fallbacks). "
                        "Set SC_EXPORT_BTN_TEXT in .env to the correct button text. "
                        "See screenshots/ for browser state."
                    )

            download: Download = await dl_info.value
            await download.save_as(str(csv_path))
            downloaded_path = csv_path
            logger.info("CSV downloaded and saved to %s", csv_path)
            await _screenshot(page, "07_download_complete")

            return downloaded_path

        except Exception as exc:
            await _screenshot(page, "error_final")
            raise RuntimeError(
                f"ShiftCare scraper failed: {exc}. "
                "Check screenshots/ for browser state at time of failure. "
                "Selector env vars: SC_SEL_EMAIL, SC_SEL_PASSWORD, SC_SEL_SUBMIT, "
                "SC_EXPORT_URL, SC_NAV_REPORTS_TEXT, SC_NAV_NOTES_TEXT, SC_EXPORT_BTN_TEXT."
            ) from exc
        finally:
            await browser.close()


async def _set_date_range(page: Page, date_str_au: str, timeout_ms: int) -> None:
    """
    Try multiple strategies to fill the from/to date pickers.
    Logs a warning instead of crashing if none work — some UIs default to today.
    """
    # Strategy 1: labelled inputs (from/to)
    date_inputs = [
        ("input[name*='from'], input[placeholder*='from'], input[id*='from_date']", "from-date"),
        ("input[name*='to'], input[placeholder*='to'], input[id*='to_date']", "to-date"),
    ]
    filled = False
    for selector, label in date_inputs:
        try:
            el = page.locator(selector).first
            if await el.count():
                await el.triple_click()
                await el.type(date_str_au)
                filled = True
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Date fill attempt failed for %s: %s", label, exc)

    # Strategy 2: date-type inputs
    if not filled:
        try:
            inputs = page.locator("input[type='date'], input[type='text'][placeholder*='/']")
            count = await inputs.count()
            if count >= 2:
                for i in range(min(2, count)):
                    await inputs.nth(i).triple_click()
                    await inputs.nth(i).type(date_str_au)
                filled = True
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Strategy 2 date fill failed: %s", exc)

    if not filled:
        logger.warning(
            "Could not fill date pickers with %s — the UI may auto-default to today. "
            "Verify the downloaded CSV covers the expected date.",
            date_str_au,
        )
    else:
        # Trigger any onchange/blur handlers
        try:
            await page.keyboard.press("Tab")
            await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
        except Exception:  # pylint: disable=broad-except
            pass


# ---------------------------------------------------------------------------
# Sync wrapper for use from non-async callers
# ---------------------------------------------------------------------------

def run_scraper(
    email: str,
    password: str,
    input_dir: Path | str,
    target_date: date | None = None,
) -> Path | None:
    """Synchronous wrapper around scrape_case_notes()."""
    return asyncio.run(scrape_case_notes(email, password, Path(input_dir), target_date))
