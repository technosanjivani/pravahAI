"""
adminresend.py
──────────────
Sends transactional emails (OTPs, team invites, etc.) using PravaahAI's own
official Resend account — never a customer's own Resend integration
(that one stays in resend.py, used only for their outreach campaigns).

Reads credentials from environment variables:

    ADMIN_RESEND_API_KEY     -> your Resend API key (starts with "re_")
    ADMIN_RESEND_FROM_EMAIL  -> the "from" address, e.g. "PravaahAI <team@pravaahai.app>"

Add both to your .env file.
"""

import os
import random
import string
import requests

# Falls back to the old PLATFORM_RESEND_* names if you already have those
# set, so nothing breaks before you update your .env.
ADMIN_RESEND_API_KEY = os.getenv("ADMIN_RESEND_API_KEY", os.getenv("PLATFORM_RESEND_API_KEY", ""))
ADMIN_RESEND_FROM_EMAIL = os.getenv(
    "ADMIN_RESEND_FROM_EMAIL",
    os.getenv("PLATFORM_RESEND_FROM", "PravaahAI <team@pravaahai.app>"),
)
RESEND_API_URL = "https://api.resend.com/emails"


def is_admin_resend_configured() -> bool:
    return bool(ADMIN_RESEND_API_KEY)


def _send_via_resend(to_address: str, subject: str, html_body: str) -> dict:
    """Low-level POST to Resend's API using PravaahAI's own account."""
    if not ADMIN_RESEND_API_KEY:
        return {"success": False, "error": "ADMIN_RESEND_API_KEY is not set in .env"}
    if not to_address:
        return {"success": False, "error": "Recipient email is required"}
    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {ADMIN_RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": ADMIN_RESEND_FROM_EMAIL,
                "to": [to_address],
                "subject": subject,
                "html": html_body,
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("message", resp.text)
            except ValueError:
                err = resp.text
            return {"success": False, "error": err, "status_code": resp.status_code}
        return {"success": True, "id": resp.json().get("id"), "status_code": resp.status_code}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Could not reach Resend: {e}"}


def send_admin_email(to_address: str, subject: str, html_body: str) -> dict:
    """Generic entry point — sends any transactional email from PravaahAI's
    own official address. Use this for anything the app itself is telling
    the user, never for outreach/marketing on behalf of a customer."""
    return _send_via_resend(to_address, subject, html_body)


# ----------------------------------
# Shared email shell (keeps every admin email visually consistent)
# ----------------------------------

def _email_shell(inner_html: str) -> str:
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;color:#0b1e3d;">
      <div style="text-align:center;margin-bottom:24px;">
        <span style="font-family:Georgia,serif;font-size:20px;font-weight:700;color:#2563eb;">PravaahAI</span>
      </div>
      {inner_html}
      <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e9f2;font-size:11.5px;color:#93a3c2;text-align:center;">
        This is an automated message from PravaahAI. Please don't reply to this email.
      </div>
    </div>
    """


# ----------------------------------
# OTP
# ----------------------------------

def generate_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def send_otp_email(to_address: str, otp_code: str, purpose: str = "verify your email") -> dict:
    """Sends a one-time password / verification code from PravaahAI's own
    official Resend account."""
    inner = f"""
      <p style="font-size:14px;color:#5c6b8a;margin-bottom:4px;">Use this code to {purpose}:</p>
      <div style="font-family:'Space Grotesk',monospace;font-size:32px;font-weight:700;
                  letter-spacing:8px;color:#0b1e3d;background:#eef3fb;border-radius:12px;
                  padding:18px 0;text-align:center;margin:16px 0;">
        {otp_code}
      </div>
      <p style="font-size:12.5px;color:#93a3c2;">This code expires in 10 minutes. If you didn't request this, you can safely ignore this email.</p>
    """
    return _send_via_resend(to_address, f"Your PravaahAI verification code: {otp_code}", _email_shell(inner))


# ----------------------------------
# Team invites
# ----------------------------------

def send_team_invite_email(to_address: str, member_name: str, temp_password: str, login_url: str, business_name: str = "") -> dict:
    """Sends a team-member invite from PravaahAI's own official Resend
    account — never from the account owner's personal Resend integration."""
    org_line = f" on <strong>{business_name}</strong>'s" if business_name else "'s"
    inner = f"""
      <p style="font-size:14px;color:#0b1e3d;">Hi {member_name or 'there'},</p>
      <p style="font-size:14px;color:#5c6b8a;">You've been added as a team member{org_line} PravaahAI account.</p>
      <div style="background:#eef3fb;border-radius:12px;padding:16px 18px;margin:18px 0;font-size:13.5px;line-height:1.9;">
        <div><strong>Login email:</strong> {to_address}</div>
        <div><strong>Temporary password:</strong> {temp_password}</div>
      </div>
      <div style="text-align:center;margin:22px 0;">
        <a href="{login_url}" style="display:inline-block;background:linear-gradient(135deg,#38bdf8,#2563eb 55%,#1e3a8a);
                  color:#fff;text-decoration:none;font-weight:600;font-size:13.5px;padding:11px 26px;border-radius:99px;">
          Log In to PravaahAI
        </a>
      </div>
      <p style="font-size:12.5px;color:#93a3c2;">Please change your password after logging in.</p>
    """
    return _send_via_resend(to_address, "You've been invited to PravaahAI", _email_shell(inner))