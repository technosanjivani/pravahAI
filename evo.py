"""
evo.py
Handles sending WhatsApp messages via the Evolution API.

IMPORTANT CHANGE:
The Evolution server URL and global API key now live ONLY in .env
(EVOLUTION_API_URL / EVOLUTION_API_KEY). Users never provide these —
each user only saves their own `instance_name` in Settings, and every
call below uses the shared URL/key automatically.
"""

import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Loaded once from .env — shared by every user/instance
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")


def send_whatsapp_message(
    instance_name: str,
    phone_number: str,
    message: str,
) -> dict:
    """
    Send a WhatsApp message using Evolution API.

    Args:
        instance_name: The user's Evolution instance name (per-user, saved in Settings).
        phone_number:  Recipient number (E.164 format, e.g. +971501234567).
        message:       Message text (already template-rendered).

    Returns:
        dict: {"success": bool, "data": dict|None, "error": str|None}
    """

    if not instance_name:
        return {"success": False, "data": None, "error": "Instance name is required."}

    if not phone_number:
        return {"success": False, "data": None, "error": "Phone number is required."}

    if not message:
        return {"success": False, "data": None, "error": "Message is required."}

    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        return {
            "success": False,
            "data": None,
            "error": "EVOLUTION_API_URL or EVOLUTION_API_KEY not configured in .env",
        }

    endpoint = f"{EVOLUTION_API_URL}/message/sendText/{instance_name}"

    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json",
    }

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

    except requests.exceptions.HTTPError:
        try:
            error = response.json()
        except Exception:
            error = {"message": response.text}
        logger.error("Evolution API HTTP Error: %s", error)
        return {
            "success": False,
            "data": error,
            "error": error.get("message", "HTTP Error"),
        }

    except requests.exceptions.RequestException as e:
        logger.exception("Evolution API Request Error")
        return {"success": False, "data": None, "error": str(e)}


def get_instance_status(instance_name: str) -> dict:
    """
    Get Evolution instance connection state (used by the "Test Connection" button).
    """

    if not instance_name:
        return {"success": False, "state": None, "error": "Instance name is required."}

    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        return {
            "success": False,
            "state": None,
            "error": "EVOLUTION_API_URL or EVOLUTION_API_KEY not configured in .env",
        }

    endpoint = f"{EVOLUTION_API_URL}/instance/connectionState/{instance_name}"
    headers = {"apikey": EVOLUTION_API_KEY}

    try:
        response = requests.get(endpoint, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        state = data.get("instance", {}).get("state") or data.get("state")
        return {"success": True, "state": state, "data": data, "error": None}

    except requests.exceptions.RequestException as e:
        logger.exception("Failed to fetch instance status")
        return {"success": False, "state": None, "error": str(e)}