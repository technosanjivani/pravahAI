"""
evolution_api.py
Handles sending WhatsApp messages via the Evolution API.
"""

import requests
import logging

logger = logging.getLogger(__name__)


def send_whatsapp_message(
    instance_name: str,
    api_url: str,
    api_key: str,
    phone_number: str,
    message: str,
) -> dict:
    """
    Send a WhatsApp text message using the Evolution API.

    Args:
        instance_name: Evolution instance name (e.g. "my-instance")
        api_url:       Base URL of the Evolution API server (e.g. "https://evo.example.com")
        api_key:       Evolution API key / token
        phone_number:  Recipient phone number in E.164 format (e.g. "+971501234567")
        message:       Text message body (supports basic WhatsApp markdown)

    Returns:
        dict with keys: success (bool), data (dict|None), error (str|None)
    """
    if not all([instance_name, api_url, api_key, phone_number, message]):
        return {"success": False, "data": None, "error": "Missing required parameters"}

    # Normalise base URL
    base = api_url.rstrip("/")

    endpoint = f"{base}/message/sendText/{instance_name}"

    headers = {
        "Content-Type": "application/json",
        "apikey": api_key,
    }

    # Evolution API v2 payload shape
    payload = {
        "number": phone_number,
        "options": {
            "delay": 1000,
            "presence": "composing",
        },
        "textMessage": {
            "text": message,
        },
    }

    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        return {"success": True, "data": response.json(), "error": None}
    except requests.exceptions.HTTPError as e:
        error_body = {}
        try:
            error_body = e.response.json()
        except Exception:
            pass
        error_msg = error_body.get("message") or str(e)
        logger.error("Evolution API HTTP error: %s", error_msg)
        return {"success": False, "data": None, "error": error_msg}
    except requests.exceptions.RequestException as e:
        logger.error("Evolution API request error: %s", e)
        return {"success": False, "data": None, "error": str(e)}


def get_instance_status(instance_name: str, api_url: str, api_key: str) -> dict:
    """Check the connection status of an Evolution instance."""
    if not all([instance_name, api_url, api_key]):
        return {"success": False, "state": None, "error": "Missing parameters"}

    base = api_url.rstrip("/")
    endpoint = f"{base}/instance/connectionState/{instance_name}"
    headers = {"apikey": api_key}

    try:
        response = requests.get(endpoint, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        state = data.get("instance", {}).get("state") or data.get("state")
        return {"success": True, "state": state, "data": data, "error": None}
    except requests.exceptions.RequestException as e:
        return {"success": False, "state": None, "error": str(e)}