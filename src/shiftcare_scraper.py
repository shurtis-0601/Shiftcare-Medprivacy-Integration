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
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

logger = logging.getLogger(__name__)

# URL fragments that indicate a 2FA / verification step
_2FA_KEYWORDS = ("2fa", "two_factor", "two-factor", "verify", "otp", "mfa")


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

async def _login(
    page: Page,
    context: BrowserContext,
    base_url: str,
    email: str,
    password: str,
    timeout_ms: int,
) -> Page:
    """
    Run the full manual login flow.  Returns the active Page after login,
    which may differ from the input page if ShiftCare opened a new tab
    during the 2FA / verification step.
    """
    sel_email = _env("SC_SEL_EMAIL", "input[type='email'], input[name='email'], #user_email")
    sel_password = _env("SC_SEL_PASSWORD", "input[type='password'], input[name='password'], #user_password")
    sel_submit = _env("SC_SEL_SUBMIT", "input[type='submit'], button[type='submit']")

    logger.info("Navigating to ShiftCare: %s (following redirect to login)", base_url)
    await page.goto(base_url, timeout=timeout_ms)
    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    await _screenshot(page, "01_login_page")
    logger.info("Login page loaded: %s", page.url)

    # ---- Email field ----
    email_input = page.locator(sel_email).first
    if not await email_input.count():
        raise RuntimeError(
            f"Could not find email input using selector {sel_email!r}. "
            "Check SC_SEL_EMAIL or see screenshots/."
        )
    logger.info(
        "Email field — selector: %r  id=%r  name=%r  type=%r  placeholder=%r",
        sel_email,
        await email_input.get_attribute("id"),
        await email_input.get_attribute("name"),
        await email_input.get_attribute("type"),
        await email_input.get_attribute("placeholder"),
    )
    # Use click + type (not fill) so React's onChange fires and its internal
    # state is updated.  fill() sets the DOM value directly and bypasses
    # synthetic input events, which causes Rails/React forms to submit blank.
    await email_input.click()
    await email_input.fill("")          # clear first
    await email_input.type(email, delay=50)
    readback = await email_input.input_value()
    logger.info("Email typed: %r — readback: %r — match: %s", email, readback, readback == email)

    # ---- Password field ----
    password_input = page.locator(sel_password).first
    if not await password_input.count():
        raise RuntimeError(
            f"Could not find password input using selector {sel_password!r}. "
            "Check SC_SEL_PASSWORD or see screenshots/."
        )
    logger.info(
        "Password field — selector: %r  id=%r  name=%r  type=%r  placeholder=%r",
        sel_password,
        await password_input.get_attribute("id"),
        await password_input.get_attribute("name"),
        await password_input.get_attribute("type"),
        await password_input.get_attribute("placeholder"),
    )
    await password_input.click()
    await password_input.fill("")
    await password_input.type(password, delay=50)
    pw_readback_len = len(await password_input.input_value())
    logger.info(
        "Password typed: %s — readback length: %d — match: %s",
        "*" * len(password), pw_readback_len, pw_readback_len == len(password),
    )
    await _screenshot(page, "02_login_filled")

    # ---- reCAPTCHA pause ----
    print(
        "\n"
        "┌─────────────────────────────────────────────────────────────┐\n"
        "│  Please tick the reCAPTCHA in the browser window, then      │\n"
        "│  press Enter here to continue.                               │\n"
        "└─────────────────────────────────────────────────────────────┘"
    )
    await asyncio.get_event_loop().run_in_executor(None, input)

    # Re-check email — readback works for text fields.
    post_email = await email_input.input_value()
    logger.info("After reCAPTCHA — email: %r (ok: %s)", post_email, post_email == email)
    if post_email != email:
        logger.info("Email was cleared — re-typing")
        await email_input.click()
        await email_input.fill("")
        await email_input.type(email, delay=50)

    # Always re-type password unconditionally.  Two reasons:
    # 1. reCAPTCHA interaction reliably clears the password field.
    # 2. input_value() returns "" for password fields regardless of content
    #    (browser security), so readback cannot confirm whether the field
    #    is filled — we cannot check, so we always re-type.
    logger.info("Re-typing password after reCAPTCHA pause (always required)")
    await password_input.click()
    await password_input.fill("")
    await password_input.type(password, delay=50)

    # Untick "Remember me" if present and checked — session lifetime is
    # managed by our own shiftcare_session.json, not the browser cookie.
    rem_sel = _env(
        "SC_SEL_REMEMBER_ME",
        "input[type='checkbox'][name*='remember'], #user_remember_me",
    )
    remember_me = page.locator(rem_sel).first
    if await remember_me.count():
        if await remember_me.is_checked():
            await remember_me.uncheck()
            logger.info("Unchecked 'Remember me'")
        else:
            logger.info("'Remember me' already unchecked")

    # ---- Submit ----
    submit_btn = page.locator(sel_submit).first
    if not await submit_btn.count():
        raise RuntimeError(
            f"Could not find submit button using selector {sel_submit!r}. "
            "Check SC_SEL_SUBMIT or see screenshots/."
        )
    logger.info(
        "Submit button — selector: %r  id=%r  type=%r  value=%r  text=%r",
        sel_submit,
        await submit_btn.get_attribute("id"),
        await submit_btn.get_attribute("type"),
        await submit_btn.get_attribute("value"),
        (await submit_btn.inner_text()).strip()[:60],
    )

    # Track any new pages ShiftCare opens (2FA popup, redirect to new tab, etc.)
    new_pages: list[Page] = []
    context.on("page", lambda p: new_pages.append(p))

    await _screenshot(page, "02b_pre_submit")
    logger.info("Clicking Sign In — current URL: %s", page.url)
    try:
        await submit_btn.click()
    except Exception as exc:  # pylint: disable=broad-except
        # TargetClosedError can fire here if the page immediately navigates
        # to a new context; log and continue — _resolve_active_page handles it.
        logger.debug("Exception during submit click (may be expected): %s", exc)

    try:
        await _screenshot(page, "02c_post_submit_click")
    except Exception:  # pylint: disable=broad-except
        pass  # page may have already closed

    # Find whichever page is now active (handles TargetClosedError / new tabs)
    active_page = await _resolve_active_page(page, new_pages, context, timeout_ms)
    logger.info("Post-submit active page: %s", active_page.url)

    # ---- 2FA / verification pause ----
    if any(kw in active_page.url.lower() for kw in _2FA_KEYWORDS):
        await _screenshot(active_page, "04_2fa_page")
        print(
            "\n"
            "┌─────────────────────────────────────────────────────────────┐\n"
            "│  2FA / verification required.  Complete it in the browser,  │\n"
            "│  then press Enter here to continue.                          │\n"
            "└─────────────────────────────────────────────────────────────┘"
        )
        await asyncio.get_event_loop().run_in_executor(None, input)
        try:
            await active_page.wait_for_url(
                lambda u: not any(kw in u.lower() for kw in _2FA_KEYWORDS),
                timeout=timeout_ms,
            )
        except Exception:  # pylint: disable=broad-except
            pass  # user may have already navigated; check URL below
        await active_page.wait_for_load_state("networkidle", timeout=timeout_ms)
        await _screenshot(active_page, "05_post_2fa")
        logger.info("Post-2FA URL: %s", active_page.url)

    # ---- Final check ----
    if "sign_in" in active_page.url:
        error_text = ""
        for err_sel in (".alert", ".alert-danger", ".error", "#error_explanation",
                        "[class*='error']", "[class*='alert']", ".flash", "p.invalid"):
            el = active_page.locator(err_sel).first
            if await el.count():
                candidate = (await el.inner_text()).strip()
                if candidate:
                    error_text = candidate
                    break
        await _screenshot(active_page, "error_login_failed")
        raise RuntimeError(
            f"Login did not complete — still on {active_page.url!r}. "
            f"Page error: {error_text!r}. "
            "Check screenshots/ for browser state."
        ) from None

    await _screenshot(active_page, "03_post_login")
    logger.info("Login successful: %s", active_page.url)
    return active_page


async def _resolve_active_page(
    original_page: Page,
    new_pages: list[Page],
    context: BrowserContext,
    timeout_ms: int,
) -> Page:
    """
    After clicking Sign In, return whichever Page is now active.

    Handles two failure modes:
    - TargetClosedError: the original page was closed/replaced during the
      redirect chain.  We find the new page from context.pages.
    - New tab: ShiftCare opened a fresh page for 2FA.  We return the newest
      page from the new_pages list captured by the context 'page' event.
    """
    try:
        await original_page.wait_for_load_state("networkidle", timeout=timeout_ms)
        return original_page
    except Exception as exc:  # pylint: disable=broad-except
        logger.info(
            "Original page unreachable after Sign In (%s) — locating new page",
            type(exc).__name__,
        )

    await asyncio.sleep(1)  # give the new page a moment to appear

    # Prefer pages captured by the context 'page' event, then any other
    # surviving page in the context.
    candidate: Page | None = new_pages[-1] if new_pages else None
    if candidate is None:
        others = [p for p in context.pages if p is not original_page]
        candidate = others[-1] if others else (context.pages[-1] if context.pages else None)

    if candidate is None:
        raise RuntimeError(
            "Original page closed after Sign In and no new page found. "
            "Check screenshots/ for browser state."
        )

    try:
        await candidate.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("New page networkidle wait failed: %s", exc)

    logger.info("Resolved active page: %s", candidate.url)
    return candidate


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

async def _save_session(context: BrowserContext, session_path: Path) -> None:
    """Persist all browser cookies to session_path as JSON."""
    try:
        cookies = await context.cookies()
        session_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        logger.info("Session saved: %d cookie(s) → %s", len(cookies), session_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not save session to %s: %s", session_path, exc)


async def _ensure_logged_in(
    page: Page,
    context: BrowserContext,
    base_url: str,
    email: str,
    password: str,
    session_path: Path,
    timeout_ms: int,
) -> Page:
    """
    Restore a saved session if one exists and is still valid; otherwise run
    the full manual login (with reCAPTCHA / 2FA pauses) and save the new
    session.  Returns the active Page (may be a new page if 2FA opened a tab).
    """
    if session_path.exists():
        logger.info("Found session file: %s — attempting restore", session_path)
        try:
            cookies = json.loads(session_path.read_text(encoding="utf-8"))
            await context.add_cookies(cookies)
            logger.info("Loaded %d cookie(s) from %s", len(cookies), session_path)

            await page.goto(base_url, timeout=timeout_ms)
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            await _screenshot(page, "01_session_check")

            if "sign_in" not in page.url:
                logger.info("Session valid — login skipped")
                return page

            logger.info("Session expired (redirected to %s) — running full login", page.url)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Could not restore session from %s (%s) — running full login", session_path, exc
            )
    else:
        logger.info("No session file at %s — running full login", session_path)

    active_page = await _login(page, context, base_url, email, password, timeout_ms)
    await _save_session(context, session_path)
    return active_page


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
    # Default to visible browser — the reCAPTCHA on the login page requires
    # manual interaction, so headless mode won't work without a CAPTCHA solver.
    # Set SHIFTCARE_HEADLESS=true only if you have an alternative auth flow.
    headless = _env("SHIFTCARE_HEADLESS", "false").lower() not in ("false", "0", "no")
    timeout_ms = _env_int("SC_NAV_TIMEOUT_MS", 30000)
    session_path = Path(_env("SHIFTCARE_SESSION_PATH", "shiftcare_session.json"))

    saved_paths: list[Path] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            page = await _ensure_logged_in(page, context, base_url, email, password, session_path, timeout_ms)
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
