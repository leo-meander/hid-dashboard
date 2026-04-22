"""Email service — Gmail SMTP for approval notifications."""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

logger = logging.getLogger(__name__)


def send_approval_email(
    reviewer_email: str,
    reviewer_name: str,
    combo_code: str,
    combo_id: str,
    branch_name: str,
    material_type: str,
    submitted_by: str,
    approval_deadline: str | None = None,
    material_link: str | None = None,
    kol_name: str | None = None,
    submitter_email: str | None = None,
) -> bool:
    """Send approval request email to reviewer via Gmail SMTP.

    Returns True if sent successfully, False otherwise.
    """
    if not settings.GMAIL_USER or not settings.GMAIL_APP_PASSWORD:
        logger.warning("Gmail credentials not configured — skipping approval email")
        return False

    review_url = f"{settings.FRONTEND_URL}/combos?review={combo_id}"

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
      <div style="background: #4F46E5; color: white; padding: 16px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin: 0; font-size: 18px;">HiD — Ad Combo Approval Request</h2>
      </div>
      <div style="border: 1px solid #E5E7EB; border-top: none; padding: 24px; border-radius: 0 0 8px 8px;">
        <p style="margin: 0 0 16px; color: #374151;">
          Hi {reviewer_name},<br/>
          A new ad combo needs your review:
        </p>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
          <tr>
            <td style="padding: 8px 12px; background: #F9FAFB; font-size: 13px; color: #6B7280; width: 40%; border: 1px solid #E5E7EB;">Combo Code</td>
            <td style="padding: 8px 12px; font-size: 13px; font-weight: 600; border: 1px solid #E5E7EB;">{combo_code}</td>
          </tr>
          <tr>
            <td style="padding: 8px 12px; background: #F9FAFB; font-size: 13px; color: #6B7280; border: 1px solid #E5E7EB;">Branch</td>
            <td style="padding: 8px 12px; font-size: 13px; border: 1px solid #E5E7EB;">{branch_name}</td>
          </tr>
          <tr>
            <td style="padding: 8px 12px; background: #F9FAFB; font-size: 13px; color: #6B7280; border: 1px solid #E5E7EB;">Type</td>
            <td style="padding: 8px 12px; font-size: 13px; border: 1px solid #E5E7EB;">{material_type}{f' (KOL: {kol_name})' if kol_name else ''}</td>
          </tr>
          <tr>
            <td style="padding: 8px 12px; background: #F9FAFB; font-size: 13px; color: #6B7280; border: 1px solid #E5E7EB;">Submitted By</td>
            <td style="padding: 8px 12px; font-size: 13px; border: 1px solid #E5E7EB;">{submitted_by}</td>
          </tr>
          <tr>
            <td style="padding: 8px 12px; background: #F9FAFB; font-size: 13px; color: #6B7280; border: 1px solid #E5E7EB;">Reviewer</td>
            <td style="padding: 8px 12px; font-size: 13px; border: 1px solid #E5E7EB;">{reviewer_name}</td>
          </tr>
          <tr>
            <td style="padding: 8px 12px; background: #F9FAFB; font-size: 13px; color: #6B7280; border: 1px solid #E5E7EB;">Approval Deadline</td>
            <td style="padding: 8px 12px; font-size: 13px; border: 1px solid #E5E7EB;">{approval_deadline or 'No deadline set'}</td>
          </tr>
          <tr>
            <td style="padding: 8px 12px; background: #F9FAFB; font-size: 13px; color: #6B7280; border: 1px solid #E5E7EB;">Material Link</td>
            <td style="padding: 8px 12px; font-size: 13px; border: 1px solid #E5E7EB;">
              {f'<a href="{material_link}" style="color: #4F46E5;">{material_link[:60]}...</a>' if material_link else '<em style="color: #9CA3AF;">(link not yet attached)</em>'}
            </td>
          </tr>
        </table>

        <div style="text-align: center; margin: 24px 0;">
          <a href="{review_url}"
             style="display: inline-block; background: #4F46E5; color: white; padding: 12px 32px;
                    border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px;">
            Review Now
          </a>
        </div>

        <p style="margin: 16px 0 0; font-size: 12px; color: #9CA3AF;">
          Click the button above to open HiD and approve or reject this combo.
        </p>
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[HiD] Approval Request — {combo_code} ({branch_name})"
    msg["From"] = settings.GMAIL_USER
    msg["To"] = reviewer_email
    # CC the submitter so they know the request was sent
    if submitter_email and submitter_email.lower() != reviewer_email.lower():
        msg["Cc"] = submitter_email
    msg.attach(MIMEText(html, "html"))

    # Build recipient list: reviewer + submitter (if different)
    recipients = [reviewer_email]
    if submitter_email and submitter_email.lower() != reviewer_email.lower():
        recipients.append(submitter_email)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(settings.GMAIL_USER, settings.GMAIL_APP_PASSWORD)
            server.sendmail(settings.GMAIL_USER, recipients, msg.as_string())
        logger.info("Approval email sent to %s for combo %s", reviewer_email, combo_code)
        return True
    except Exception as e:
        logger.error("Failed to send approval email: %s", e)
        return False
