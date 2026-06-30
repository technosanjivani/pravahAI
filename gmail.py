"""
gmail_sender.py
Sends emails via Gmail SMTP using an app password.
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587


def send_gmail(
    gmail_address: str,
    app_password: str,
    to_address: str,
    subject: str,
    body_html: str,
    body_text: str = "",
    from_name: str = "",
) -> dict:
    """
    Send an email via Gmail SMTP.

    Args:
        gmail_address: Sender's Gmail address
        app_password:  Gmail App Password (16-char, no spaces needed)
        to_address:    Recipient email address
        subject:       Email subject line
        body_html:     HTML email body
        body_text:     Plain-text fallback (auto-generated if empty)
        from_name:     Display name for the sender

    Returns:
        dict with keys: success (bool), error (str|None)
    """
    if not all([gmail_address, app_password, to_address, subject, body_html]):
        return {"success": False, "error": "Missing required email parameters"}

    from_name = from_name or gmail_address
    plain_text = body_text or _strip_html(body_html)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{gmail_address}>"
    msg["To"] = to_address

    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    # Remove spaces from app password (users often paste with spaces)
    clean_password = app_password.replace(" ", "")

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(gmail_address, clean_password)
            server.sendmail(gmail_address, to_address, msg.as_string())
        return {"success": True, "error": None}
    except smtplib.SMTPAuthenticationError:
        msg = "Gmail authentication failed. Check your App Password (not your regular password)."
        logger.error(msg)
        return {"success": False, "error": msg}
    except smtplib.SMTPException as e:
        logger.error("Gmail SMTP error: %s", e)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Unexpected Gmail error: %s", e)
        return {"success": False, "error": str(e)}


def _strip_html(html: str) -> str:
    """Very basic HTML-to-text fallback."""
    import re
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()