"""Shared HTML email sender — Resend / SendGrid HTTP, Gmail SMTP dev fallback.

Production runs on Zeabur (and similar PaaS providers) which block outbound
SMTP traffic — Gmail SMTP fails with `Errno 101 Network is unreachable`.
Resend and SendGrid use HTTPS (port 443) so they always succeed. The Gmail
SMTP path is kept only as a local-dev fallback.

Provider selection (first match wins):
  1. RESEND_API_KEY + EMAIL_FROM      → Resend HTTP API (preferred)
  2. SENDGRID_API_KEY + EMAIL_FROM    → SendGrid HTTP API
  3. GMAIL_USER + GMAIL_APP_PASSWORD  → Gmail SMTP (dev only)
  4. nothing configured               → returns False

All callers should treat this as fire-and-forget: it logs internally and
returns a bool so the HTTP-layer caller decides whether to 502 or just
record a warning.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def send_email_html(
    subject: str,
    html: str,
    to: list[str],
    cc: Optional[list[str]] = None,
) -> bool:
    """Send a single HTML email. Returns True on success, False on failure."""
    to = [r.strip() for r in (to or []) if r and r.strip()]
    cc = [r.strip() for r in (cc or []) if r and r.strip()]
    if not to:
        logger.warning("send_email_html: no recipients — aborting")
        return False

    email_from = (getattr(settings, "EMAIL_FROM", "") or "").strip()

    rs_key = (getattr(settings, "RESEND_API_KEY", "") or "").strip()
    if rs_key and email_from:
        return _send_via_resend(subject, html, to, cc, rs_key, email_from)

    sg_key = (getattr(settings, "SENDGRID_API_KEY", "") or "").strip()
    if sg_key and email_from:
        return _send_via_sendgrid(subject, html, to, cc, sg_key, email_from)

    gmail_user = (getattr(settings, "GMAIL_USER", "") or "").strip()
    gmail_pass = (getattr(settings, "GMAIL_APP_PASSWORD", "") or "").strip()
    if gmail_user and gmail_pass:
        logger.info("send_email_html: no Resend/SendGrid key — falling back to Gmail SMTP")
        return _send_via_gmail_smtp(subject, html, to, cc, gmail_user, gmail_pass)

    logger.error(
        "send_email_html: no provider configured — set RESEND_API_KEY+EMAIL_FROM "
        "(or SENDGRID_API_KEY+EMAIL_FROM) for production, or "
        "GMAIL_USER+GMAIL_APP_PASSWORD for dev."
    )
    return False


def _send_via_resend(subject, html, to, cc, api_key, from_email) -> bool:
    """Resend HTTP API — production path. Always reachable from Zeabur."""
    try:
        import resend

        resend.api_key = api_key
        params = {
            "from": from_email,
            "to": to,
            "subject": subject,
            "html": html,
        }
        if cc:
            params["cc"] = cc

        result = resend.Emails.send(params)
        # Resend returns {"id": "..."} on success, raises on failure
        msg_id = result.get("id") if isinstance(result, dict) else None
        if msg_id:
            logger.info(
                "Resend OK id=%s to=%d cc=%d subject=%s",
                msg_id, len(to), len(cc), subject,
            )
            return True
        logger.error("Resend unexpected response: %r", result)
        return False
    except Exception as e:
        logger.error("Resend send failed: %s", e)
        return False


def _send_via_sendgrid(subject, html, to, cc, api_key, from_email) -> bool:
    """SendGrid HTTP API — production path. Always reachable from Zeabur."""
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Cc, Content, Email, Mail, Personalization, To,
        )

        mail = Mail()
        mail.from_email = Email(from_email)
        mail.subject = subject

        personalization = Personalization()
        for r in to:
            personalization.add_to(To(r))
        for r in cc:
            personalization.add_cc(Cc(r))
        mail.add_personalization(personalization)

        mail.add_content(Content("text/html", html))

        sg = SendGridAPIClient(api_key)
        resp = sg.send(mail)
        if 200 <= resp.status_code < 300:
            logger.info(
                "SendGrid OK status=%s to=%d cc=%d subject=%s",
                resp.status_code, len(to), len(cc), subject,
            )
            return True
        logger.error(
            "SendGrid non-2xx status=%s headers=%s body=%s",
            resp.status_code, resp.headers, resp.body,
        )
        return False
    except Exception as e:
        logger.error("SendGrid send failed: %s", e)
        return False


def _send_via_gmail_smtp(subject, html, to, cc, gmail_user, gmail_pass) -> bool:
    """Gmail SMTP — dev/local fallback only. Won't work on Zeabur (port blocked)."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = gmail_user
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.attach(MIMEText(html, "html"))

        all_recipients = to + cc
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, all_recipients, msg.as_string())
        logger.info("Gmail SMTP OK to=%d cc=%d subject=%s", len(to), len(cc), subject)
        return True
    except Exception as e:
        logger.error("Gmail SMTP send failed: %s", e)
        return False
