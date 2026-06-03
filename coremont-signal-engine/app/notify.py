"""Email delivery for the daily digest (SMTP / Gmail app-password friendly)."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from . import config


def send_digest_email(subject: str, html_body: str, text_body: str) -> dict:
    """Send the digest via SMTP. Returns a status dict; never raises on config
    absence (so the daily task degrades gracefully to file-only output)."""
    smtp = config.smtp_config()
    recipients = config.digest_recipients()

    if smtp is None:
        return {"sent": False, "reason": "SMTP not configured (set SMTP_USER / SMTP_PASSWORD)"}
    if not recipients:
        return {"sent": False, "reason": "no recipients (set DIGEST_TO)"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp["from_addr"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(smtp["user"], smtp["password"])
        server.send_message(msg)

    return {"sent": True, "recipients": recipients, "subject": subject}
