"""
resend_sender.py
Sends emails via the Resend API (https://resend.com).
Supports both personal domain sending and Resend's shared domain.
"""

import requests
import logging

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def send_resend_email(
    api_key: str,
    from_address: str,
    to_address: str,
    subject: str,
    body_html: str,
    body_text: str = "",
    reply_to: str = "",
) -> dict:
    """
    Send an email via the Resend API.

    Args:
        api_key:      Resend API key (starts with "re_")
        from_address: Verified sender address or "Name <email@domain.com>"
        to_address:   Recipient email
        subject:      Email subject
        body_html:    HTML email body
        body_text:    Plain-text fallback (optional)
        reply_to:     Reply-to address (optional)

    Returns:
        dict with keys: success (bool), id (str|None), error (str|None)
    """
    if not all([api_key, from_address, to_address, subject, body_html]):
        return {"success": False, "id": None, "error": "Missing required parameters"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "from": from_address,
        "to": [to_address],
        "subject": subject,
        "html": body_html,
    }

    if body_text:
        payload["text"] = body_text

    if reply_to:
        payload["reply_to"] = reply_to

    try:
        response = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        return {"success": True, "id": data.get("id"), "error": None}
    except requests.exceptions.HTTPError as e:
        error_body = {}
        try:
            error_body = e.response.json()
        except Exception:
            pass
        error_msg = error_body.get("message") or str(e)
        logger.error("Resend API HTTP error: %s | body: %s", error_msg, error_body)
        return {"success": False, "id": None, "error": error_msg}
    except requests.exceptions.RequestException as e:
        logger.error("Resend request error: %s", e)
        return {"success": False, "id": None, "error": str(e)}


def verify_resend_key(api_key: str) -> dict:
    """
    Quick check: list domains to verify the API key is valid.
    Returns dict with success (bool) and error (str|None).
    """
    if not api_key:
        return {"success": False, "error": "No API key provided"}

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get("https://api.resend.com/domains", headers=headers, timeout=10)
        if response.status_code == 401:
            return {"success": False, "error": "Invalid API key"}
        response.raise_for_status()
        return {"success": True, "error": None}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}