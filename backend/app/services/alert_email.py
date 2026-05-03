"""Alert email digest — morning summary of CRITICAL alerts via shared sender."""
import logging

from app.config import settings
from app.models.alert import AlertHistory
from app.services.email_sender import send_email_html

logger = logging.getLogger(__name__)

SEVERITY_COLORS = {
    "CRITICAL": "#DC2626",
    "WARNING": "#D97706",
    "INFO": "#2563EB",
}


def send_alert_digest_email(alerts: list[AlertHistory], branch_map: dict) -> bool:
    """Send a single digest email with all alerts grouped by branch.

    Returns True if sent successfully, False otherwise. Email transport
    is handled by send_email_html (SendGrid in prod, Gmail SMTP in dev).
    """
    recipients_str = settings.EMAIL_RECIPIENTS or ""
    recipients = [e.strip() for e in recipients_str.split(",") if e.strip()]
    if not recipients:
        logger.warning("EMAIL_RECIPIENTS not configured — skipping alert digest email")
        return False

    alert_date = alerts[0].alert_date if alerts else "today"

    # Group by branch
    by_branch: dict[str, list] = {}
    for a in alerts:
        name = branch_map.get(a.branch_id, "Unknown")
        by_branch.setdefault(name, []).append(a)

    # Build alert rows HTML
    rows_html = ""
    for branch_name in sorted(by_branch.keys()):
        branch_alerts = sorted(by_branch[branch_name],
                               key=lambda x: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}.get(x.severity, 3))
        for a in branch_alerts:
            color = SEVERITY_COLORS.get(a.severity, "#6B7280")
            rows_html += f"""
            <tr>
              <td style="padding: 10px 12px; border: 1px solid #E5E7EB; font-size: 13px;">
                <strong>{branch_name}</strong>
              </td>
              <td style="padding: 10px 12px; border: 1px solid #E5E7EB; text-align: center;">
                <span style="display: inline-block; padding: 2px 10px; border-radius: 12px;
                             background: {color}; color: white; font-size: 11px; font-weight: 600;">
                  {a.severity}
                </span>
              </td>
              <td style="padding: 10px 12px; border: 1px solid #E5E7EB; font-size: 13px;">
                {a.message}
              </td>
              <td style="padding: 10px 12px; border: 1px solid #E5E7EB; font-size: 12px; color: #374151;">
                {a.recommendation}
              </td>
            </tr>"""

    critical_count = sum(1 for a in alerts if a.severity == "CRITICAL")
    warning_count = sum(1 for a in alerts if a.severity == "WARNING")
    info_count = sum(1 for a in alerts if a.severity == "INFO")

    dashboard_url = f"{settings.FRONTEND_URL}/alerts" if settings.FRONTEND_URL else "#"

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
      <div style="background: #DC2626; color: white; padding: 16px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin: 0; font-size: 18px;">HiD — Daily Performance Alerts</h2>
        <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">{alert_date}</p>
      </div>
      <div style="border: 1px solid #E5E7EB; border-top: none; padding: 24px; border-radius: 0 0 8px 8px;">

        <div style="display: flex; gap: 12px; margin-bottom: 20px;">
          <div style="flex: 1; background: #FEF2F2; border: 1px solid #FECACA; border-radius: 8px; padding: 12px; text-align: center;">
            <div style="font-size: 24px; font-weight: 700; color: #DC2626;">{critical_count}</div>
            <div style="font-size: 11px; color: #991B1B; text-transform: uppercase;">Critical</div>
          </div>
          <div style="flex: 1; background: #FFFBEB; border: 1px solid #FDE68A; border-radius: 8px; padding: 12px; text-align: center;">
            <div style="font-size: 24px; font-weight: 700; color: #D97706;">{warning_count}</div>
            <div style="font-size: 11px; color: #92400E; text-transform: uppercase;">Warning</div>
          </div>
          <div style="flex: 1; background: #EFF6FF; border: 1px solid #BFDBFE; border-radius: 8px; padding: 12px; text-align: center;">
            <div style="font-size: 24px; font-weight: 700; color: #2563EB;">{info_count}</div>
            <div style="font-size: 11px; color: #1E40AF; text-transform: uppercase;">Info</div>
          </div>
        </div>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
          <thead>
            <tr style="background: #F9FAFB;">
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: left; font-size: 12px; color: #6B7280;">Branch</th>
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: center; font-size: 12px; color: #6B7280;">Severity</th>
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: left; font-size: 12px; color: #6B7280;">Alert</th>
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: left; font-size: 12px; color: #6B7280;">Recommended Action</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>

        <div style="text-align: center; margin: 24px 0;">
          <a href="{dashboard_url}"
             style="display: inline-block; background: #4F46E5; color: white; padding: 12px 32px;
                    border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px;">
            View All Alerts in Dashboard
          </a>
        </div>

        <p style="margin: 16px 0 0; font-size: 11px; color: #9CA3AF; text-align: center;">
          This is an automated alert from HiD — Hotel Intelligence Dashboard.
          Review and acknowledge alerts in the dashboard to dismiss them.
        </p>
      </div>
    </div>
    """

    subject = (f"[HiD] Daily Alert Digest — {critical_count} Critical, "
               f"{warning_count} Warning ({alert_date})")
    ok = send_email_html(subject, html, recipients)
    if ok:
        logger.info("Alert digest email sent to %d recipients — %d alerts",
                    len(recipients), len(alerts))
    else:
        logger.error("Alert digest email send failed (%d alerts)", len(alerts))
    return ok
