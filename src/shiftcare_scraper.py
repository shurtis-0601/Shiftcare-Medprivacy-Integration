"""
Playwright-based browser automation to download per-participant PDF reports
from ShiftCare via the Download Center.

Workflow for each participant on the Events page:
  1. Login
  2. Reports → Events (participant list)
  3. Click participant → progress notes page
  4. Set date filter to target_date → click Export
     (ShiftCare queues an async job in the Download Center)
  5. Navigate to /users/report/download_center
  6. Poll the top entry until status contains "Completed"
  7. Download the PDF → save as YYYY-MM-DD-PART-XXX.pdf

All selectors and text labels are overridable via environment variables so
minor ShiftCare UI changes can be fixed without touching code.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

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
# Login
# ---------------------------------------------------------------------------

async def _login(page: Page, base_url: str, email: str, password: str, timeout_ms: int) -> None:
    sel_email = _env("SC_SEL_EMAIL", "input[type='email'], input[name='email'], #user_email")
    sel_password = _env("SC_SEL_PASSWORD", "input[type='password'], input[name='password'], #user_password")
    sel_submit = _env("SC_SEL_SUBMIT", "input[type='submit'], button[type='submit']")

    logger.info("Navigating to ShiftCare login")
    await page.goto(f"{base_url}/users/sign_in", timeout=timeout_ms)
    await _screenshot(page, "01_login_page")

    email_input = page.locator(sel_email).first
    if not await email_input.count():
        raise RuntimeError("Could not find email input. Check SC_SEL_EMAIL.")
    await email_input.fill(email)

    password_input = page.locator(sel_password).first
    if not await password_input.count():
        raise RuntimeError("Could not find password input. Check SC_SEL_PASSWORD.")
    await password_input.fill(password)
    await _screenshot(page, "02_login_filled")

    submit_btn = page.locator(sel_submit).first
    if not await submit_btn.count():
        raise RuntimeError("Could not find submit button. Check SC_SEL_SUBMIT.")
    await submit_btn.click()
    await page.wait_for_url(lambda u: "sign_in" not in u, timeout=timeout_ms)
    await _screenshot(page, "03_post_login")
    logger.info("Login successful: %s", page.url)


# ---------------------------------------------------------------------------
# Events page — participant list
# ---------------------------------------------------------------------------

async def _navigate_to_events_page(page: Page, base_url: str, timeout_ms: int) -> None:
    """Navigate to Reports → Events via the sidebar menu, or directly via SC_EVENTS_URL."""
    events_url = _env("SC_EVENTS_URL", "")
    if events_url:
        await page.goto(events_url, timeout=timeout_ms)
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    else:
        nav_reports = _env("SC_NAV_REPORTS_TEXT", "Reports")
        nav_events = _env("SC_NAV_EVENTS_TEXT", "Events")

        reports_link = page.get_by_text(nav_reports, exact=False).first
        if not await reports_link.count():
            raise RuntimeError(
                f"Could not find '{nav_reports}' in the sidebar. "
                "Set SC_EVENTS_URL in .env to navigate directly, "
                "or set SC_NAV_REPORTS_TEXT to the correct label."
            )
        await reports_link.click()
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        await _screenshot(page, "04_reports_menu")

        events_link = page.get_by_text(nav_events, exact=False).first
        if not await events_link.count():
            raise RuntimeError(
                f"Could not find '{nav_events}' under Reports. "
                "Set SC_NAV_EVENTS_TEXT or SC_EVENTS_URL in .env."
            )
        await events_link.click()
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)

    await _screenshot(page, "05_events_page")
    logger.info("Events page: %s", page.url)


async def _get_participant_links(page: Page) -> list[tuple[str, str]]:
    """
    Return (full_name, href) for every participant listed on the Events page.
    Selector is overridable via SC_SEL_PARTICIPANT_ROW.
    """
    sel = _env(
        "SC_SEL_PARTICIPANT_ROW",
        "table tbody tr a, .client-list a, .participant-row a, [data-client-name] a",
    )
    links = page.locator(sel)
    count = await links.count()
    if count == 0:
        raise RuntimeError(
            f"No participant links found using selector '{sel}'. "
            "Set SC_SEL_PARTICIPANT_ROW in .env to the correct CSS selector. "
            "See screenshots/ for the current browser state."
        )

    results: list[tuple[str, str]] = []
    for i in range(count):
        el = links.nth(i)
        name = (await el.inner_text()).strip()
        href = await el.get_attribute("href") or ""
        if name and href:
            results.append((name, href))

    logger.info("Found %d participant(s) on Events page", len(results))
    return results


# ---------------------------------------------------------------------------
# Per-participant: navigate, set date filter, trigger export
# ---------------------------------------------------------------------------

async def _export_participant_pdf(
    page: Page,
    base_url: str,
    href: str,
    target_date: date,
    timeout_ms: int,
) -> None:
    """Go to a participant's notes page, filter to target_date, click Export."""
    url = href if href.startswith("http") else f"{base_url}{href}"
    await page.goto(url, timeout=timeout_ms)
    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    await _screenshot(page, "06_participant_notes")
    logger.info("Participant notes page: %s", page.url)

    await _set_date_filter(page, target_date.strftime("%d/%m/%Y"), timeout_ms)
    await _screenshot(page, "07_date_filtered")

    export_btn_text = _env("SC_EXPORT_BTN_TEXT", "Export")
    fallback_sels = [
        "button:has-text('Export')",
        "a:has-text('Export')",
        "button:has-text('PDF')",
        "a:has-text('PDF')",
        "[data-action*='export']",
    ]
    clicked = False
    btn = page.get_by_text(export_btn_text, exact=False).first
    if await btn.count():
        await btn.click()
        clicked = True
    else:
        for sel in fallback_sels:
            el = page.locator(sel).first
            if await el.count():
                await el.click()
                clicked = True
                logger.info("Used fallback export selector: %s", sel)
                break

    if not clicked:
        raise RuntimeError(
            f"Could not find export button (tried text='{export_btn_text}' and fallbacks). "
            "Set SC_EXPORT_BTN_TEXT in .env. See screenshots/."
        )

    await _screenshot(page, "08_export_clicked")
    logger.info("Export triggered — job queued in Download Center")
    await page.wait_for_timeout(2000)  # brief pause for the server to register the job


async def _set_date_filter(page: Page, date_str_au: str, timeout_ms: int) -> None:
    """Fill from/to date inputs with the same target date (single-day range)."""
    date_sels = [
        "input[name*='from'], input[placeholder*='from' i], input[id*='from_date']",
        "input[name*='to'],   input[placeholder*='to' i],   input[id*='to_date']",
    ]
    filled = False
    for sel in date_sels:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.triple_click()
                await el.type(date_str_au)
                filled = True
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Date fill failed for '%s': %s", sel, exc)

    if not filled:
        try:
            inputs = page.locator("input[type='date'], input[type='text'][placeholder*='/']")
            count = await inputs.count()
            for i in range(min(2, count)):
                await inputs.nth(i).triple_click()
                await inputs.nth(i).type(date_str_au)
            filled = count > 0
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Date fallback fill failed: %s", exc)

    if filled:
        try:
            await page.keyboard.press("Tab")
            await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
        except Exception:  # pylint: disable=broad-except
            pass
    else:
        logger.warning(
            "Could not fill date filter with %s — verify screenshots confirm date coverage.",
            date_str_au,
        )


# ---------------------------------------------------------------------------
# Download Center — poll until Completed, then download
# ---------------------------------------------------------------------------

async def _wait_and_download(
    page: Page,
    base_url: str,
    input_dir: Path,
    filename: str,
    timeout_ms: int,
) -> Path:
    """
    Navigate to the Download Center, wait for the top entry to show Completed,
    then download it and save as input_dir/filename.

    Polls every SC_DC_POLL_INTERVAL_MS ms (default 3 s).
    Gives up after SC_DC_POLL_MAX_ATTEMPTS attempts (default 40 = ~2 min).
    """
    dc_url = _env("SC_DOWNLOAD_CENTER_URL", f"{base_url}/users/report/download_center")
    poll_interval_ms = _env_int("SC_DC_POLL_INTERVAL_MS", 3000)
    poll_max_attempts = _env_int("SC_DC_POLL_MAX_ATTEMPTS", 40)
    sel_rows = _env("SC_SEL_DC_ROW", "table tbody tr, .download-item, [data-download-id]")
    sel_status = _env("SC_SEL_DC_STATUS", "td.status, .status-badge, [data-status]")
    sel_dl_btn = _env(
        "SC_SEL_DC_DOWNLOAD",
        "a:has-text('Download'), button:has-text('Download'), a.download-link",
    )
    completed_text = _env("SC_DC_COMPLETED_TEXT", "Completed")

    logger.info("Navigating to Download Center: %s", dc_url)
    await page.goto(dc_url, timeout=timeout_ms)
    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    await _screenshot(page, "09_download_center")

    for attempt in range(1, poll_max_attempts + 1):
        rows = page.locator(sel_rows)
        if not await rows.count():
            logger.warning(
                "No entries in Download Center — attempt %d/%d", attempt, poll_max_attempts
            )
            await page.wait_for_timeout(poll_interval_ms)
            await page.reload()
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            continue

        first_row = rows.first
        status_el = first_row.locator(sel_status).first
        status_text = (
            (await status_el.inner_text()).strip()
            if await status_el.count()
            else (await first_row.inner_text()).strip()
        )

        logger.info(
            "Download Center — attempt %d/%d, top-row status: %r",
            attempt, poll_max_attempts, status_text[:80],
        )

        if completed_text.lower() in status_text.lower():
            await _screenshot(page, "10_dc_completed")
            break

        if attempt == poll_max_attempts:
            await _screenshot(page, "error_dc_timeout")
            raise RuntimeError(
                f"Download Center did not reach '{completed_text}' after "
                f"{poll_max_attempts} attempts "
                f"({poll_max_attempts * poll_interval_ms // 1000} s). "
                f"Last status: {status_text!r}. "
                "Adjust SC_DC_POLL_MAX_ATTEMPTS or SC_DC_COMPLETED_TEXT in .env."
            )

        await page.wait_for_timeout(poll_interval_ms)
        await page.reload()
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)

    # Click the Download button in the completed (top) row
    first_row = page.locator(sel_rows).first
    dl_btn = first_row.locator(sel_dl_btn).first
    if not await dl_btn.count():
        dl_btn = page.locator(sel_dl_btn).first  # page-level fallback

    if not await dl_btn.count():
        raise RuntimeError(
            "Could not find Download button in the completed entry. "
            "Check SC_SEL_DC_DOWNLOAD in .env. See screenshots/."
        )

    dest = input_dir / filename
    async with page.expect_download(timeout=timeout_ms * 2) as dl_info:
        await dl_btn.click()
    download = await dl_info.value
    await download.save_as(str(dest))
    logger.info("PDF saved: %s", dest)
    await _screenshot(page, "11_download_done")
    return dest


# ---------------------------------------------------------------------------
# Client ID helper — matches csv_ingestor so ref map codes stay consistent
# ---------------------------------------------------------------------------

def _client_id_from_name(full_name: str) -> str:
    return f"csv_{hashlib.md5(full_name.lower().encode()).hexdigest()[:8]}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def scrape_all_participants(
    email: str,
    password: str,
    input_dir: Path,
    target_date: date,
    ref_map,
) -> list[Path]:
    """
    Download a PDF report for every participant on the ShiftCare Events page.

    Files are saved to input_dir as {target_date}-{PART-XXX}.pdf.
    Existing files are skipped (idempotent).
    Per-participant failures are logged and skipped so the rest continues.
    Returns the list of PDF paths that were saved (or already existed).
    """
    input_dir = Path(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)

    base_url = _env("SHIFTCARE_BASE_URL", "https://app.shiftcare.com").rstrip("/")
    headless = _env("SHIFTCARE_HEADLESS", "true").lower() not in ("false", "0", "no")
    timeout_ms = _env_int("SC_NAV_TIMEOUT_MS", 30000)

    saved_paths: list[Path] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            await _login(page, base_url, email, password, timeout_ms)
            await _navigate_to_events_page(page, base_url, timeout_ms)
            participant_links = await _get_participant_links(page)
            events_page_url = page.url

            for full_name, href in participant_links:
                logger.info("Processing participant: %s", full_name)

                # Assign or look up the PART-XXX code from the reference map
                parts = full_name.strip().split(None, 1)
                client_id = _client_id_from_name(full_name)
                ref_code = ref_map.get_or_create_code(
                    client_id,
                    {
                        "first_name": parts[0] if parts else full_name,
                        "last_name": parts[1] if len(parts) > 1 else "",
                    },
                )

                filename = f"{target_date.isoformat()}-{ref_code}.pdf"
                dest = input_dir / filename
                if dest.exists():
                    logger.info("PDF already exists for %s — skipping: %s", ref_code, dest)
                    saved_paths.append(dest)
                    await page.goto(events_page_url, timeout=timeout_ms)
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    continue

                try:
                    await _export_participant_pdf(
                        page, base_url, href, target_date, timeout_ms
                    )
                    path = await _wait_and_download(
                        page, base_url, input_dir, filename, timeout_ms
                    )
                    saved_paths.append(path)
                except RuntimeError as exc:
                    logger.error(
                        "Failed to export PDF for %s (%s): %s", full_name, ref_code, exc
                    )

                # Return to the Events page for the next participant
                await page.goto(events_page_url, timeout=timeout_ms)
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)

            logger.info(
                "Scrape complete: %d/%d PDFs in %s",
                len(saved_paths), len(participant_links), input_dir,
            )

        except Exception as exc:
            await _screenshot(page, "error_final")
            raise RuntimeError(
                f"ShiftCare scraper failed: {exc}. "
                "Check screenshots/ for browser state. "
                "Key env vars: SC_SEL_EMAIL, SC_SEL_PASSWORD, SC_EVENTS_URL, "
                "SC_SEL_PARTICIPANT_ROW, SC_EXPORT_BTN_TEXT, SC_DOWNLOAD_CENTER_URL, "
                "SC_SEL_DC_ROW, SC_SEL_DC_STATUS, SC_SEL_DC_DOWNLOAD, SC_DC_COMPLETED_TEXT."
            ) from exc
        finally:
            await browser.close()

    return saved_paths


def run_scraper(
    email: str,
    password: str,
    input_dir: Path | str,
    target_date: date,
    ref_map,
) -> list[Path]:
    """Synchronous wrapper around scrape_all_participants()."""
    return asyncio.run(
        scrape_all_participants(email, password, Path(input_dir), target_date, ref_map)
    )
