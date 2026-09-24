"""
wirebaseapi.py — thin client for Wirebase's Developer (multi-tenant) API.
PravaahAI = ONE developer app. Every PravaahAI customer = ONE tenant
(externalUserId = customer's Mongo _id) with ONE WhatsApp instance.
"""
import os
import re
import requests


def _cfg():
    return (
        os.getenv("WIREBASE_DEV_BASE_URL", "").rstrip("/"),
        os.getenv("WIREBASE_APP_SECRET", ""),
    )


def is_configured() -> bool:
    base, secret = _cfg()
    return bool(base and secret)


def _call(method, path, json_body=None, params=None, timeout=20):
    base, secret = _cfg()
    if not (base and secret):
        return {"success": False, "error": "WIREBASE_DEV_BASE_URL / WIREBASE_APP_SECRET missing in server .env"}
    try:
        resp = requests.request(
            method, f"{base}/api/v1/developer{path}",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            json=json_body, params=params, timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Could not reach Wirebase: {e}"}

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        err = (body.get("error") or body.get("message")) if isinstance(body, dict) else None
        return {
            "success": False,
            "error": str(err) if err else f"Wirebase returned HTTP {resp.status_code}",
            "status_code": resp.status_code, "raw": body,
        }
    return {"success": True, "data": body, "status_code": resp.status_code}


def upsert_tenant(external_user_id, name, webhook_url=""):
    body = {"externalUserId": str(external_user_id), "name": name or str(external_user_id)}
    if webhook_url:
        body["webhookUrl"] = webhook_url
    return _call("POST", "/tenants", body)


def create_instance(external_user_id, name, webhook_url=""):
    body = {"name": name}
    if webhook_url:
        body["webhookUrl"] = webhook_url
    return _call("POST", f"/tenants/{external_user_id}/instances", body)


def connect_instance(instance_id):
    return _call("POST", f"/instances/{instance_id}/connect")


def get_status(instance_id):
    return _call("GET", f"/instances/{instance_id}/status")


def get_qr(instance_id):
    return _call("GET", f"/instances/{instance_id}/qr")


def send_text(instance_id, to, message):
    digits = re.sub(r"\D", "", to or "")
    if not digits:
        return {"success": False, "error": "Invalid phone number"}
    return _call("POST", f"/instances/{instance_id}/messages",
                 {"to": digits, "type": "text", "message": message})


def extract_qr(data) -> str:
    """The /qr response shape isn't in the docs, so accept the common ones:
    a data-URL, a bare base64 PNG, or a raw WhatsApp QR string."""
    if isinstance(data, str):
        candidate = data.strip()
    elif isinstance(data, dict):
        candidate = ""
        for k in ("qr", "qrCode", "qrcode", "qr_code", "base64", "image", "dataUrl", "data"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                candidate = v.strip()
                break
            if isinstance(v, dict):
                nested = extract_qr(v)
                if nested:
                    return nested
    else:
        return ""
    if candidate.startswith("iVBOR"):            # bare base64 PNG
        return "data:image/png;base64," + candidate
    return candidate