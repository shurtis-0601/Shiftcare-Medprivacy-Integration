"""
Email notifications via the Gmail API.

Requires domain-wide delegation:
  1. In Google Workspace Admin: Security → API Controls → Domain-wide delegation
     → Add the service account client ID with scope:
     https://www.googleapis.com/auth/gmail.send
  2. Set GMAIL_SENDER_EMAIL env var to the address that sends the notification.

If GMAIL_SENDER_EMAIL is not set, notification is skipped and a warning is logged.
The pipeline still runs successfully — email is informational only.
"""
from __future__ import annotations

import base64
import logging
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth import default as google_auth_default
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config) -> None:
        self._sender = config.gmail_sender
        self._recipient = config.notification_email

    def send_pipeline_report(
        self,
        target_date: date,
        stats: dict,
        quarantined: list[dict],
        errors: list[dict],
    ) -> None:
        if not self._sender:
            logger.warning(
                "GMAIL_SENDER_EMAIL not set — skipping email notification. "
                "Stats: %s  Quarantined: %d  Errors: %d",
                stats, len(quarantined), len(errors),
            )
            return

        subject = (
            f"[MedPrivacy Pipeline] {target_date} — "
            f"{stats.get('quarantined', 0)} quarantined, {stats.get('errors', 0)} errors"
        )
        html = self._build_html(target_date, stats, quarantined, errors)

        try:
            creds, _ = google_auth_default(
                scopes=["https://www.googleapis.com/auth/gmail.send"]
            )
            # Impersonate the sender via domain-wide delegation if using a
            # service account (requires DWD to be configured in Workspace Admin).
            if hasattr(creds, "with_subject"):
                creds = creds.with_subject(self._sender)

            svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
            raw = self._encode_message(self._sender, self._recipient, subject, html)
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            logger.info("Notification email sent to %s", self._recipient)
        except HttpError as exc:
            logger.error("Failed to send notification email: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_message(sender: str, recipient: str, subject: str, html: str) -> str:
        msg = MIMEMultipart("alternative")
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html"))
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    @staticmethod
    def _build_html(
        target_date: date,
        stats: dict,
        quarantined: list[dict],
        errors: list[dict],
    ) -> str:
        q_rows = "".join(
            f"<tr><td>{q['note_id']}</td><td>{q['ref_code']}</td>"
            f"<td>{q.get('reason', '')}</td></tr>"
            for q in quarantined
        )
        e_rows = "".join(
            f"<tr><td>{e.get('note_id', 'N/A')}</td><td>{e.get('error', '')}</td></tr>"
            for e in errors
        )
        q_table = (
            f"<h3>Quarantined Notes ({len(quarantined)})</h3>"
            f"<table border='1' cellpadding='4'>"
            f"<tr><th>Note ID</th><th>Ref Code</th><th>Reason</th></tr>{q_rows}</table>"
            if quarantined else "<p>No quarantined notes.</p>"
        )
        e_table = (
            f"<h3>Errors ({len(errors)})</h3>"
            f"<table border='1' cellpadding='4'>"
            f"<tr><th>Note ID</th><th>Error</th></tr>{e_rows}</table>"
            if errors else "<p>No errors.</p>"
        )
        return f"""
        <html><body>
        <h2>MedPrivacy Pipeline Report — {target_date}</h2>
        <table border='1' cellpadding='4'>
          <tr><th>Metric</th><th>Count</th></tr>
          <tr><td>Total notes fetched</td><td>{stats.get('total', 0)}</td></tr>
          <tr><td>Uploaded to Pending</td><td>{stats.get('uploaded', 0)}</td></tr>
          <tr><td>Quarantined</td><td>{stats.get('quarantined', 0)}</td></tr>
          <tr><td>Skipped (already processed)</td><td>{stats.get('skipped', 0)}</td></tr>
          <tr><td>Errors</td><td>{stats.get('errors', 0)}</td></tr>
        </table>
        {q_table}
        {e_table}
        <hr><p style='color:grey;font-size:11px;'>
        This is an automated notification from the ShiftCare → MedPrivacy → Google Drive pipeline.
        Quarantined notes require manual review before they can be moved to the Pending folder.
        </p>
        </body></html>
        """
