"""
ShiftCare API v3 client.

Endpoints used:
  GET /api/v3/clients          — full participant list (paginated)
  GET /api/v3/service_notes    — case/progress notes filtered by date (paginated)

Authentication: Authorization: Token token="<API_KEY>"
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

_RETRYABLE = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


class ShiftCareError(Exception):
    pass


class ShiftCareClient:
    """Thin wrapper around the ShiftCare V3 REST API."""

    PER_PAGE = 100
    TIMEOUT = 30  # seconds

    def __init__(self, config) -> None:
        self._base = config.shiftcare_base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f'Token token="{config.shiftcare_api_key}"',
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_clients(self) -> dict[int, dict[str, Any]]:
        """Return all active clients keyed by their ShiftCare integer ID."""
        clients: dict[int, dict[str, Any]] = {}
        for page_data in self._paginate("/api/v3/clients"):
            for c in page_data.get("clients", []):
                clients[c["id"]] = c
        logger.info("Fetched %d clients from ShiftCare", len(clients))
        return clients

    def get_employees(self) -> list[dict[str, Any]]:
        """
        Return all active employees/support workers.
        Used by the deidentifier to replace staff names with [STAFF_NAME].
        Returns an empty list gracefully if the endpoint is not available.
        """
        staff: list[dict[str, Any]] = []
        try:
            for page_data in self._paginate("/api/v3/employees"):
                staff.extend(page_data.get("employees", []))
        except ShiftCareError as exc:
            logger.warning("Could not fetch employees (non-fatal): %s", exc)
        logger.info("Fetched %d employees from ShiftCare", len(staff))
        return staff

    def get_service_notes(self, start: date, end: date) -> list[dict[str, Any]]:
        """Return all service/progress notes for the given inclusive date range."""
        params = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        notes: list[dict[str, Any]] = []
        for page_data in self._paginate("/api/v3/service_notes", extra_params=params):
            notes.extend(page_data.get("service_notes", []))
        logger.info(
            "Fetched %d service notes for %s – %s", len(notes), start, end
        )
        return notes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _paginate(
        self, path: str, extra_params: dict | None = None
    ):
        """Yield one response dict per page until all pages are consumed."""
        page = 1
        while True:
            params = {"page": page, "per_page": self.PER_PAGE}
            if extra_params:
                params.update(extra_params)
            data = self._get(path, params)
            yield data

            meta = data.get("meta", {})
            total = meta.get("total_count", 0)
            per_page = meta.get("per_page", self.PER_PAGE)
            current = meta.get("current_page", page)

            # Stop when we've consumed all pages or the page returned no meta
            if not meta or (current * per_page) >= total:
                break
            page += 1

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get(self, path: str, params: dict) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            raise ShiftCareError(
                f"ShiftCare API returned HTTP {status} for {url}: {exc}"
            ) from exc
