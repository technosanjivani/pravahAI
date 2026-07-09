from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    flash,
    url_for,
    jsonify,
    Response,
    send_file,
)

from functools import wraps

from pymongo import MongoClient
from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv

from datetime import datetime, timedelta
import os
import io
import csv
import re
import json as _json
import secrets
import random
import string
import threading
import time

import pandas as pd

# Sender modules
from evo import send_whatsapp_message, get_instance_status
from gmail import send_gmail
from resend import send_resend_email, verify_resend_key
import requests
# ----------------------------------
# Load Environment Variables
# ----------------------------------

load_dotenv()

# ----------------------------------
# Flask App
# ----------------------------------

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# ----------------------------------
# MongoDB
# ----------------------------------

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(MONGO_URI)
db = client["pravahai"]

users_col      = db["pravah-users"]
leads_col      = db["pravah-leads"]
campaigns_col  = db["pravah-campaigns"]
executions_col = db["pravah-executions"]   # per-lead campaign execution logs
messages_col   = db["pravah-messages"]     # inbound/outbound whatsapp message log
teams_col      = db["pravah-team"]         # team members (sub-accounts)

# ----------------------------------
# Create Indexes
# ----------------------------------

try:
    users_col.create_index("username", unique=True)
    users_col.create_index("email", unique=True)
    users_col.create_index("webhook_token", unique=True, sparse=True)
    leads_col.create_index("owner_id")
    leads_col.create_index([("owner_id", 1), ("created_at", -1)])
    leads_col.create_index([("owner_id", 1), ("phone", 1)])
    campaigns_col.create_index("owner_id")
    executions_col.create_index([("campaign_id", 1), ("lead_id", 1)])
    executions_col.create_index("owner_id")
    messages_col.create_index([("owner_id", 1), ("lead_id", 1), ("created_at", -1)])
    teams_col.create_index("owner_id")
    teams_col.create_index("email", unique=True)
except Exception:
    pass

# ----------------------------------
# Lead Import Settings
# ----------------------------------

ALLOWED_IMPORT_EXTENSIONS = {"csv", "xlsx", "xls"}

LEAD_TEMPLATE_HEADERS = [
    "Name", "Business Name", "Email", "Phone", "Website", "Description",
]

IMPORT_HEADER_ALIASES = {
    "name": "name", "leadname": "name", "fullname": "name", "contactname": "name",
    "businessname": "business_name", "business": "business_name",
    "company": "business_name", "companyname": "business_name",
    "email": "email", "emailaddress": "email",
    "phone": "phone", "phonenumber": "phone", "number": "phone",
    "mobile": "phone", "contactnumber": "phone",
    "website": "website", "url": "website", "site": "website",
    "description": "description", "notes": "description",
    "note": "description", "details": "description",
}

LEAD_STATUSES = {"cold", "warm", "hot"}

# ----------------------------------
# Campaign throttling settings
# ----------------------------------

CAMPAIGN_BATCH_SIZE        = int(os.getenv("CAMPAIGN_BATCH_SIZE", 25))       # leads sent per burst
CAMPAIGN_BATCH_WAIT_MIN    = int(os.getenv("CAMPAIGN_BATCH_WAIT_MIN", 120))  # seconds
CAMPAIGN_BATCH_WAIT_MAX    = int(os.getenv("CAMPAIGN_BATCH_WAIT_MAX", 300))  # seconds
CAMPAIGN_AI_GEN_WAIT_SECS  = int(os.getenv("CAMPAIGN_AI_GEN_WAIT_SECS", 120))  # gap between AI generations

# Tracks the last time an AI generation happened per-owner, so consecutive
# AI-personalized messages inside a running campaign are spaced out.
_last_ai_call_lock = threading.Lock()
_last_ai_call_time = {}  # owner_id -> epoch seconds


def throttle_ai_call(owner_id: str):
    """Blocks the calling thread until enough time has passed since the last
    AI generation for this owner, so we don't hammer the AI or the channel."""
    with _last_ai_call_lock:
        last = _last_ai_call_time.get(owner_id, 0)
    now = time.time()
    elapsed = now - last
    if elapsed < CAMPAIGN_AI_GEN_WAIT_SECS:
        time.sleep(CAMPAIGN_AI_GEN_WAIT_SECS - elapsed)
    with _last_ai_call_lock:
        _last_ai_call_time[owner_id] = time.time()


def normalize_header(h):
    return str(h).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def normalize_phone(phone: str) -> str:
    """Keep digits only, so +971 50 123 4567, 971501234567 and
    971501234567@s.whatsapp.net all compare equal on their last 9-10 digits."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def generate_webhook_token() -> str:
    return secrets.token_urlsafe(24)


def generate_temp_password(length=10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ----------------------------------
# Auth Helpers
# ----------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect("/login")
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    """Restricts a route to the account owner (not team members)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect("/login")
        if session.get("role") != "owner":
            return jsonify({"error": "This section is only available to the account owner"}), 403
        return view(*args, **kwargs)
    return wrapped


def current_user_id():
    """The owner_id that all data (leads/campaigns/etc) is scoped under —
    the same for the owner and for any of their team members."""
    return session.get("user_id")


def current_actor_id():
    """The id of whoever is actually logged in (owner or team member).
    Used for assignment / 'my leads' filtering."""
    return session.get("actor_id", session.get("user_id"))


def is_owner():
    return session.get("role") == "owner"


# ----------------------------------
# Serialization Helpers
# ----------------------------------

def serialize_lead(lead):
    return {
        "_id": str(lead["_id"]),
        "name": lead.get("name", ""),
        "business_name": lead.get("business_name", ""),
        "email": lead.get("email", ""),
        "phone": lead.get("phone", ""),
        "website": lead.get("website", ""),
        "description": lead.get("description", ""),
        "source": lead.get("source", "manual"),
        "status": lead.get("status", "cold"),
        "assigned_to": lead.get("assigned_to"),
        "ai_task_prompt": lead.get("ai_task_prompt", ""),
        "created_at": lead.get("created_at").isoformat() if lead.get("created_at") else None,
        "updated_at": lead.get("updated_at").isoformat() if lead.get("updated_at") else None,
    }


def clean_lead_payload(data):
    return {
        "name": (data.get("name") or "").strip(),
        "business_name": (data.get("business_name") or "").strip(),
        "email": (data.get("email") or "").strip().lower(),
        "phone": (data.get("phone") or "").strip(),
        "website": (data.get("website") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "ai_task_prompt": (data.get("ai_task_prompt") or "").strip(),
    }


def serialize_campaign(c):
    return {
        "_id": str(c["_id"]),
        "name": c.get("name", ""),
        "description": c.get("description", ""),
        "status": c.get("status", "draft"),
        "flow": c.get("flow", []),
        "lead_ids": c.get("lead_ids", []),
        "created_at": c.get("created_at").isoformat() if c.get("created_at") else None,
        "updated_at": c.get("updated_at").isoformat() if c.get("updated_at") else None,
        "last_run_at": c.get("last_run_at").isoformat() if c.get("last_run_at") else None,
        "stats": c.get("stats", {"sent": 0, "failed": 0, "pending": 0}),
    }


def serialize_execution(e):
    return {
        "_id": str(e["_id"]),
        "campaign_id": e.get("campaign_id", ""),
        "lead_id": e.get("lead_id", ""),
        "lead_name": e.get("lead_name", ""),
        "step_index": e.get("step_index", 0),
        "status": e.get("status", "pending"),
        "channel": e.get("channel", ""),
        "error": e.get("error", ""),
        "executed_at": e.get("executed_at").isoformat() if e.get("executed_at") else None,
    }


def serialize_team_member(m):
    return {
        "_id": str(m["_id"]),
        "name": m.get("name", ""),
        "email": m.get("email", ""),
        "status": m.get("status", "active"),
        "leads_assigned": leads_col.count_documents({"assigned_to": str(m["_id"])}),
        "created_at": m.get("created_at").isoformat() if m.get("created_at") else None,
    }


def serialize_message(m):
    return {
        "_id": str(m["_id"]),
        "lead_id": m.get("lead_id", ""),
        "direction": m.get("direction", "in"),
        "channel": m.get("channel", "whatsapp"),
        "text": m.get("text", ""),
        "created_at": m.get("created_at").isoformat() if m.get("created_at") else None,
    }


# ----------------------------------
# Template variable substitution
# ----------------------------------

def render_template_vars(text: str, lead: dict) -> str:
    """Replace {{name}}, {{email}}, etc. with lead field values."""
    replacements = {
        "{{name}}": lead.get("name", ""),
        "{{business_name}}": lead.get("business_name", ""),
        "{{email}}": lead.get("email", ""),
        "{{phone}}": lead.get("phone", ""),
        "{{website}}": lead.get("website", ""),
        "{{description}}": lead.get("description", ""),
    }
    for key, val in replacements.items():
        text = text.replace(key, val)
    return text


def _mistral_chat(system_prompt: str, user_prompt: str, force_json: bool = False):
    """Low-level Mistral call shared by all AI helpers below."""
    MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
    MISTRAL_URL   = "https://api.mistral.ai/v1/chat/completions"
    MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
    if not MISTRAL_API_KEY:
        return {"success": False, "error": "MISTRAL_API_KEY not configured"}
    try:
        resp = requests.post(
            MISTRAL_URL,
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MISTRAL_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.7,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if force_json:
            cleaned = text.strip().strip("```json").strip("```").strip()
            return {"success": True, "data": _json.loads(cleaned)}
        return {"success": True, "text": text}
    except Exception as e:
        return {"success": False, "error": str(e)}


def generate_ai_content(lead: dict, content_type: str, instructions: str = "", custom_system_prompt: str = "") -> dict:
    """
    Calls Mistral with the lead's full data (name -> description) and returns
    a generated WhatsApp message, or an email subject+body.

    If custom_system_prompt is provided (the user's own prompt from Settings),
    it replaces PravaahAI's default style instructions. For emails we still
    force JSON output on top of it, since the app needs subject+body separately.
    """
    lead_context = (
        f"Name: {lead.get('name','')}\n"
        f"Business Name: {lead.get('business_name','')}\n"
        f"Email: {lead.get('email','')}\n"
        f"Phone: {lead.get('phone','')}\n"
        f"Website: {lead.get('website','')}\n"
        f"Description: {lead.get('description','')}\n"
    )

    custom_system_prompt = (custom_system_prompt or "").strip()

    if content_type == "whatsapp":
        default_prompt = (
            "You are a sales outreach assistant. Write a short, friendly, personalized "
            "WhatsApp message (2-4 sentences) to this lead using their real data below. "
            "No placeholders. Return ONLY the message text, nothing else."
        )
        system_prompt = custom_system_prompt or default_prompt
        system_prompt += "\n\nReturn ONLY the WhatsApp message text, nothing else."
    else:
        default_prompt = (
            "You are a sales outreach assistant. Write a personalized outreach email "
            "for this lead using their real data below."
        )
        base = custom_system_prompt or default_prompt
        system_prompt = (
            base
            + '\n\nNo matter what, return ONLY valid JSON in the exact shape '
              '{"subject": "...", "body": "..."} with no markdown fences and no extra text. '
              "Body may use simple HTML paragraph tags."
        )

    user_prompt = f"Lead data:\n{lead_context}"
    if instructions:
        user_prompt += f"\nAdditional instructions: {instructions}\n"

    result = _mistral_chat(system_prompt, user_prompt, force_json=(content_type != "whatsapp"))
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "AI generation failed")}

    if content_type == "whatsapp":
        return {"success": True, "message": result["text"]}

    parsed = result["data"]
    return {"success": True, "subject": parsed.get("subject", ""), "body": parsed.get("body", "")}


def generate_chat_reply(lead: dict, incoming_message: str, history: list, task_prompt: str = "", system_prompt: str = "") -> dict:
    """Generates a WhatsApp auto-reply to an inbound message, using the
    lead's assigned task prompt (what this lead should be pitched / how the
    conversation should be steered) plus the account's general WA style prompt."""
    lead_context = (
        f"Name: {lead.get('name','')}\n"
        f"Business Name: {lead.get('business_name','')}\n"
        f"Email: {lead.get('email','')}\n"
        f"Phone: {lead.get('phone','')}\n"
        f"Website: {lead.get('website','')}\n"
        f"Description: {lead.get('description','')}\n"
    )

    history_text = "\n".join(
        f"{'Lead' if h.get('direction')=='in' else 'You'}: {h.get('text','')}"
        for h in history[-10:]
    )

    base_style = (system_prompt or "").strip() or (
        "You are a friendly, concise WhatsApp sales assistant replying to an inbound lead message."
    )
    task = (task_prompt or "").strip() or "Keep the lead engaged and move the conversation toward a sale or a booked call."

    system = (
        f"{base_style}\n\n"
        f"Your specific goal for this lead: {task}\n\n"
        "Reply in 1-3 short sentences like a real person texting on WhatsApp. "
        "No signatures, no placeholders. Return ONLY the reply text."
    )
    user_prompt = (
        f"Lead data:\n{lead_context}\n"
        f"Recent conversation:\n{history_text}\n\n"
        f"Lead's latest message: {incoming_message}\n\n"
        "Write your reply now."
    )

    result = _mistral_chat(system, user_prompt, force_json=False)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "AI reply generation failed")}
    return {"success": True, "message": result["text"]}


def classify_lead_temperature(lead: dict, incoming_message: str, history: list, current_status: str = "cold") -> str:
    """Asks the AI to classify a lead as cold / warm / hot based on the
    conversation so far. Falls back to the current status on any failure."""
    history_text = "\n".join(
        f"{'Lead' if h.get('direction')=='in' else 'You'}: {h.get('text','')}"
        for h in history[-10:]
    )
    system = (
        "You are a sales-lead scoring assistant. Based on the WhatsApp conversation, "
        "classify how interested/ready-to-buy this lead currently is. "
        "Respond with EXACTLY one word: cold, warm, or hot. Nothing else."
    )
    user_prompt = (
        f"Conversation so far:\n{history_text}\n"
        f"Lead's latest message: {incoming_message}\n\n"
        "Classification (one word: cold, warm, or hot):"
    )
    result = _mistral_chat(system, user_prompt, force_json=False)
    if not result.get("success"):
        return current_status if current_status in LEAD_STATUSES else "cold"
    word = result["text"].strip().lower()
    for status in LEAD_STATUSES:
        if status in word:
            return status
    return current_status if current_status in LEAD_STATUSES else "cold"


# ----------------------------------
# Team round-robin assignment
# ----------------------------------

def assign_round_robin(owner_id: str):
    """Picks the next active team member in rotation and returns their id
    as a string, or None if the account has no team members."""
    members = list(teams_col.find({"owner_id": owner_id, "status": "active"}).sort("created_at", 1))
    if not members:
        return None
    owner = users_col.find_one({"_id": ObjectId(owner_id)})
    idx = (owner.get("team_rr_index", 0) if owner else 0) % len(members)
    chosen = members[idx]
    users_col.update_one({"_id": ObjectId(owner_id)}, {"$inc": {"team_rr_index": 1}})
    return str(chosen["_id"])


# ----------------------------------
# Campaign Flow Execution Engine
# ----------------------------------

def execute_campaign_for_lead(campaign: dict, lead: dict, user: dict, owner_id: str):
    flow = campaign.get("flow", [])
    campaign_id = str(campaign["_id"])
    lead_id = str(lead["_id"])

    creds = user.get("integrations", {})
    evo_instance = creds.get("evo_instance", "")
    gmail_addr   = creds.get("gmail_address", "")
    gmail_pass   = creds.get("gmail_app_password", "")
    resend_key   = creds.get("resend_api_key", "")
    resend_from  = creds.get("resend_from_address", "")
    ai_wa_prompt    = creds.get("ai_whatsapp_prompt", "")
    ai_email_prompt = creds.get("ai_email_prompt", "")

    step_idx = 0
    while step_idx < len(flow):
        step = flow[step_idx]
        step_type = step.get("type", "")

        if step_type == "wait":
            amount = float(step.get("amount", 1) or 1)
            unit = step.get("unit", "minutes")
            seconds = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(unit, 60) * amount
            time.sleep(min(seconds, 3600))  # safety cap so a single wait step can't block forever

        elif step_type == "condition":
            field = step.get("field", "")
            operator = step.get("operator", "exists")
            value = (step.get("value") or "").strip().lower()
            field_val = str(lead.get(field, "") or "").strip().lower()
            if operator == "exists":
                result = bool(field_val)
            elif operator == "not_exists":
                result = not bool(field_val)
            elif operator == "equals":
                result = field_val == value
            elif operator == "contains":
                result = value in field_val
            elif operator == "not_contains":
                result = value not in field_val
            else:
                result = True
            next_step = step.get("then_step") if result else step.get("else_step")
            if next_step is not None:
                step_idx = next_step
                continue

        elif step_type == "whatsapp":
            phone   = lead.get("phone", "")
            message = step.get("message", "")

            if step.get("use_ai"):
                throttle_ai_call(owner_id)
                ai_result = generate_ai_content(lead, "whatsapp", step.get("ai_instructions", ""), ai_wa_prompt)
                if ai_result.get("success"):
                    message = ai_result.get("message", message)

            message = render_template_vars(message, lead)

            if not evo_instance or not phone:
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), step_idx, "failed", "whatsapp", "Missing WhatsApp instance or phone number")
                _bump_campaign_stat(campaign["_id"], "failed")
            else:
                send_result = send_whatsapp_message(evo_instance, phone, message)
                if send_result.get("success"):
                    messages_col.insert_one({
                        "owner_id": owner_id, "lead_id": lead_id, "direction": "out",
                        "channel": "whatsapp", "text": message, "created_at": datetime.utcnow(),
                    })
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), step_idx, "sent", "whatsapp")
                    _bump_campaign_stat(campaign["_id"], "sent")
                else:
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), step_idx, "failed", "whatsapp", send_result.get("error", "Send failed"))
                    _bump_campaign_stat(campaign["_id"], "failed")

        elif step_type == "email":
            provider = step.get("provider", "gmail")
            to_addr  = lead.get("email", "")
            subject  = step.get("subject", "")
            body     = step.get("body", "")

            if step.get("use_ai"):
                throttle_ai_call(owner_id)
                ai_result = generate_ai_content(lead, "email", step.get("ai_instructions", ""), ai_email_prompt)
                if ai_result.get("success"):
                    subject = ai_result.get("subject", subject)
                    body    = ai_result.get("body", body)

            subject = render_template_vars(subject, lead)
            body    = render_template_vars(body, lead)
            channel_name = f"email_{provider}"

            if not to_addr:
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), step_idx, "failed", channel_name, "Lead has no email address")
                _bump_campaign_stat(campaign["_id"], "failed")
            else:
                try:
                    if provider == "resend":
                        if not resend_key or not resend_from:
                            raise Exception("Resend not configured in Settings")
                        send_resend_email(resend_key, resend_from, to_addr, subject, body)
                    else:
                        if not gmail_addr or not gmail_pass:
                            raise Exception("Gmail not configured in Settings")
                        send_gmail(gmail_addr, gmail_pass, to_addr, subject, body)
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), step_idx, "sent", channel_name)
                    _bump_campaign_stat(campaign["_id"], "sent")
                except Exception as e:
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), step_idx, "failed", channel_name, str(e))
                    _bump_campaign_stat(campaign["_id"], "failed")

        step_idx += 1


def _bump_campaign_stat(campaign_oid, status):
    field = "stats.sent" if status == "sent" else "stats.failed"
    campaigns_col.update_one({"_id": campaign_oid}, {"$inc": {field: 1}})


def _log_execution(owner_id, campaign_id, lead_id, lead_name, step_index, status, channel, error=""):
    executions_col.insert_one({
        "owner_id":    owner_id,
        "campaign_id": campaign_id,
        "lead_id":     lead_id,
        "lead_name":   lead_name,
        "step_index":  step_index,
        "status":      status,
        "channel":     channel,
        "error":       error or "",
        "executed_at": datetime.utcnow(),
    })


def launch_campaign(campaign_id: str, owner_id: str):
    campaign = campaigns_col.find_one({"_id": ObjectId(campaign_id), "owner_id": owner_id})
    if not campaign:
        return False

    user = users_col.find_one({"_id": ObjectId(owner_id)})
    if not user:
        return False

    lead_ids = campaign.get("lead_ids", [])
    if not lead_ids:
        return False

    leads = list(leads_col.find({
        "_id": {"$in": [ObjectId(lid) for lid in lead_ids]},
        "owner_id": owner_id
    }))

    campaigns_col.update_one(
        {"_id": campaign["_id"]},
        {"$set": {
            "status": "running",
            "last_run_at": datetime.utcnow(),
            "stats": {"sent": 0, "failed": 0, "pending": len(leads)},
        }}
    )

    def run():
        # Send in bursts of CAMPAIGN_BATCH_SIZE, immediately for the first
        # burst, then wait a random 2-5 min before the next burst so we don't
        # blast every lead at once and look like spam / trip WhatsApp limits.
        for i in range(0, len(leads), CAMPAIGN_BATCH_SIZE):
            batch = leads[i:i + CAMPAIGN_BATCH_SIZE]
            for lead in batch:
                execute_campaign_for_lead(campaign, lead, user, owner_id)
                campaigns_col.update_one({"_id": campaign["_id"]}, {"$inc": {"stats.pending": -1}})
            if i + CAMPAIGN_BATCH_SIZE < len(leads):
                time.sleep(random.uniform(CAMPAIGN_BATCH_WAIT_MIN, CAMPAIGN_BATCH_WAIT_MAX))
        campaigns_col.update_one({"_id": campaign["_id"]}, {"$set": {"status": "completed"}})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return True


# ==================================================================
# HOME / AUTH ROUTES
# ==================================================================

@app.route("/")
def home():
    if "user_id" in session:
        return redirect("/dashboard")
    return redirect("/login")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username      = request.form.get("username", "").strip().lower()
        email         = request.form.get("email", "").strip().lower()
        phone         = request.form.get("phone", "").strip()
        business_name = request.form.get("business_name", "").strip()
        business_type = request.form.get("business_type", "").strip()
        password      = request.form.get("password", "")

        if not username:
            flash("Username required"); return redirect("/signup")
        if not password:
            flash("Password required"); return redirect("/signup")

        if users_col.find_one({"username": username, "type": "user"}):
            flash("Username already exists"); return redirect("/signup")
        if users_col.find_one({"email": email, "type": "user"}):
            flash("Email already exists"); return redirect("/signup")

        users_col.insert_one({
            "type": "user",
            "username": username,
            "email": email,
            "phone": phone,
            "business_name": business_name,
            "business_type": business_type,
            "website": "",
            "address": "",
            "password": generate_password_hash(password),
            "status": "active",
            "email_verified": False,
            "plan": {"name": "Free", "credits": 100},
            "integrations": {},
            "webhook_token": generate_webhook_token(),
            "team_rr_index": 0,
            "created_at": datetime.utcnow(),
            "last_login": None,
        })
        flash("Account created successfully")
        return redirect("/login")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        # Try account owner first
        user = users_col.find_one({"username": username, "type": "user"})
        if user and check_password_hash(user["password"], password):
            session["user_id"]  = str(user["_id"])
            session["actor_id"] = str(user["_id"])
            session["username"] = user["username"]
            session["role"]     = "owner"
            users_col.update_one({"_id": user["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
            return redirect("/dashboard")

        # Try team member (logs in with email as "username")
        member = teams_col.find_one({"email": username})
        if member and check_password_hash(member["password"], password) and member.get("status") == "active":
            session["user_id"]  = member["owner_id"]           # data is scoped to the owner
            session["actor_id"] = str(member["_id"])
            session["username"] = member.get("name") or member["email"]
            session["role"]     = "member"
            teams_col.update_one({"_id": member["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
            return redirect("/dashboard")

        flash("Invalid username or password"); return redirect("/login")
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    if not user:
        session.clear(); return redirect("/login")
    return render_template("dashboard.html", user=user, role=session.get("role", "owner"), display_name=session.get("username"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/me", methods=["GET"])
@login_required
def api_me():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "role": session.get("role", "owner"),
        "display_name": session.get("username"),
        "business_name": user.get("business_name", ""),
        "username": user.get("username", ""),
    })


# ==================================================================
# LEADS API
# ==================================================================

def _leads_scope_query():
    """Owners see every lead in the account; team members only see leads
    that have been assigned to them (e.g. via round-robin on reply)."""
    query = {"owner_id": current_user_id()}
    if not is_owner():
        query["assigned_to"] = current_actor_id()
    return query


@app.route("/api/leads", methods=["GET"])
@login_required
def api_list_leads():
    q = request.args.get("q", "").strip()
    query = _leads_scope_query()
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"business_name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
        ]
    leads = list(leads_col.find(query).sort("created_at", -1))
    return jsonify({"leads": [serialize_lead(l) for l in leads]})


@app.route("/api/leads", methods=["POST"])
@login_required
def api_create_lead():
    data = request.get_json(silent=True) or {}
    lead = clean_lead_payload(data)
    if not lead["name"]:
        return jsonify({"error": "Name is required"}), 400
    lead["owner_id"]    = current_user_id()
    lead["source"]      = "manual"
    lead["status"]      = "cold"
    lead["assigned_to"] = None
    lead["created_at"]  = datetime.utcnow()
    lead["updated_at"]  = datetime.utcnow()
    result = leads_col.insert_one(lead)
    saved  = leads_col.find_one({"_id": result.inserted_id})
    return jsonify({"lead": serialize_lead(saved)}), 201


@app.route("/api/leads/bulk", methods=["POST"])
@login_required
def api_bulk_save_leads():
    data = request.get_json(silent=True) or {}
    rows = data.get("leads", [])
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "No leads provided"}), 400
    saved_leads, skipped = [], 0
    for row in rows:
        lead = clean_lead_payload(row)
        if not lead["name"]: skipped += 1; continue
        lead_id = row.get("_id")
        if lead_id:
            try: oid = ObjectId(lead_id)
            except InvalidId: skipped += 1; continue
            lead["updated_at"] = datetime.utcnow()
            leads_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": lead})
            updated = leads_col.find_one({"_id": oid, "owner_id": current_user_id()})
            if updated: saved_leads.append(serialize_lead(updated))
            else: skipped += 1
        else:
            lead["owner_id"]    = current_user_id()
            lead["source"]      = row.get("source", "manual")
            lead["status"]      = "cold"
            lead["assigned_to"] = None
            lead["created_at"]  = datetime.utcnow()
            lead["updated_at"]  = datetime.utcnow()
            result = leads_col.insert_one(lead)
            created = leads_col.find_one({"_id": result.inserted_id})
            saved_leads.append(serialize_lead(created))
    return jsonify({"leads": saved_leads, "saved": len(saved_leads), "skipped": skipped})


@app.route("/api/leads/<lead_id>", methods=["PUT", "PATCH"])
@login_required
def api_update_lead(lead_id):
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    data = request.get_json(silent=True) or {}
    lead = clean_lead_payload(data)
    if not lead["name"]: return jsonify({"error": "Name is required"}), 400
    lead["updated_at"] = datetime.utcnow()
    result = leads_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": lead})
    if result.matched_count == 0: return jsonify({"error": "Lead not found"}), 404
    updated = leads_col.find_one({"_id": oid})
    return jsonify({"lead": serialize_lead(updated)})


@app.route("/api/leads/<lead_id>/status", methods=["PATCH"])
@login_required
def api_update_lead_status(lead_id):
    """Lets the owner OR the assigned team member manually override a
    lead's hot/warm/cold status from the sheet."""
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in LEAD_STATUSES:
        return jsonify({"error": "status must be cold, warm, or hot"}), 400
    query = {"_id": oid, "owner_id": current_user_id()}
    if not is_owner():
        query["assigned_to"] = current_actor_id()
    result = leads_col.update_one(query, {"$set": {"status": status, "updated_at": datetime.utcnow()}})
    if result.matched_count == 0:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify({"updated": True, "status": status})


@app.route("/api/leads/<lead_id>", methods=["DELETE"])
@login_required
def api_delete_lead(lead_id):
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    result = leads_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0: return jsonify({"error": "Lead not found"}), 404
    return jsonify({"deleted": True})


@app.route("/api/leads/import", methods=["POST"])
@login_required
def api_import_leads():
    if "file" not in request.files: return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if not file.filename: return jsonify({"error": "No file selected"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        return jsonify({"error": "Unsupported file type. Please upload .csv, .xlsx or .xls"}), 400
    try:
        df = pd.read_csv(file) if ext == "csv" else pd.read_excel(file)
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400
    column_map = {}
    for col in df.columns:
        key = normalize_header(col)
        if key in IMPORT_HEADER_ALIASES:
            column_map[col] = IMPORT_HEADER_ALIASES[key]
    if "name" not in column_map.values():
        return jsonify({"error": "Could not find a 'Name' column."}), 400
    df = df.rename(columns=column_map)
    inserted, skipped, now, docs = 0, 0, datetime.utcnow(), []
    for _, row in df.iterrows():
        name = str(row.get("name", "") or "").strip()
        if not name or name.lower() == "nan": skipped += 1; continue
        def clean(field):
            val = row.get(field, "")
            return "" if pd.isna(val) else str(val).strip()
        docs.append({
            "owner_id": current_user_id(), "name": name,
            "business_name": clean("business_name"), "email": clean("email").lower(),
            "phone": clean("phone"), "website": clean("website"),
            "description": clean("description"), "source": "import",
            "status": "cold", "assigned_to": None, "ai_task_prompt": "",
            "created_at": now, "updated_at": now,
        })
        inserted += 1
    if docs: leads_col.insert_many(docs)
    return jsonify({"inserted": inserted, "skipped": skipped})


@app.route("/api/leads/template", methods=["GET"])
@login_required
def api_leads_template():
    """Downloadable starter template — as a real Excel file so it opens
    nicely and keeps column formatting."""
    df = pd.DataFrame(
        [["Bhuvi Patel", "Al Noor Spices Trading LLC", "info@alnoorspices.ae",
          "+971 50 123 4567", "https://alnoorspices.ae", "Importer looking for bulk basmati rice"]],
        columns=LEAD_TEMPLATE_HEADERS,
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Leads")
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="pravaahai_leads_template.xlsx",
    )


@app.route("/api/leads/export", methods=["GET"])
@login_required
def api_leads_export():
    """Export the current lead list (respecting the same scoping as the
    leads list view) as an Excel file."""
    leads = list(leads_col.find(_leads_scope_query()).sort("created_at", -1))
    rows = [{
        "Name": l.get("name", ""), "Business Name": l.get("business_name", ""),
        "Email": l.get("email", ""), "Phone": l.get("phone", ""),
        "Website": l.get("website", ""), "Description": l.get("description", ""),
        "Status": l.get("status", "cold"),
    } for l in leads]
    df = pd.DataFrame(rows, columns=["Name", "Business Name", "Email", "Phone", "Website", "Description", "Status"])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Leads")
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="pravaahai_leads_export.xlsx",
    )


@app.route("/api/leads/<lead_id>/messages", methods=["GET"])
@login_required
def api_lead_messages(lead_id):
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    query = {"_id": oid, "owner_id": current_user_id()}
    if not is_owner():
        query["assigned_to"] = current_actor_id()
    lead = leads_col.find_one(query)
    if not lead: return jsonify({"error": "Lead not found"}), 404
    msgs = list(messages_col.find({"owner_id": current_user_id(), "lead_id": lead_id}).sort("created_at", 1))
    return jsonify({"messages": [serialize_message(m) for m in msgs]})


@app.route("/api/leads/<lead_id>/send-whatsapp", methods=["POST"])
@login_required
def api_send_manual_whatsapp(lead_id):
    """Lets the owner or the lead's assigned team member send a one-off
    manual WhatsApp message from the lead detail view."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message text is required"}), 400
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400

    query = {"_id": oid, "owner_id": current_user_id()}
    if not is_owner():
        query["assigned_to"] = current_actor_id()
    lead = leads_col.find_one(query)
    if not lead: return jsonify({"error": "Lead not found"}), 404
    if not lead.get("phone"):
        return jsonify({"error": "This lead has no phone number"}), 400

    owner = users_col.find_one({"_id": ObjectId(current_user_id())})
    evo_instance = (owner.get("integrations", {}) or {}).get("evo_instance", "") if owner else ""
    if not evo_instance:
        return jsonify({"error": "WhatsApp instance not configured in Settings"}), 400

    result = send_whatsapp_message(evo_instance, lead["phone"], message)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "Send failed")}), 400

    messages_col.insert_one({
        "owner_id": current_user_id(), "lead_id": lead_id, "direction": "out",
        "channel": "whatsapp", "text": message, "created_at": datetime.utcnow(),
    })
    return jsonify({"sent": True})


# ==================================================================
# CAMPAIGNS API
# ==================================================================

@app.route("/api/campaigns", methods=["GET"])
@login_required
@owner_required
def api_list_campaigns():
    campaigns = list(campaigns_col.find({"owner_id": current_user_id()}).sort("created_at", -1))
    return jsonify({"campaigns": [serialize_campaign(c) for c in campaigns]})


@app.route("/api/campaigns", methods=["POST"])
@login_required
@owner_required
def api_create_campaign():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Campaign name is required"}), 400
    doc = {
        "owner_id":   current_user_id(),
        "name":       name,
        "description": (data.get("description") or "").strip(),
        "status":     "draft",
        "flow":       data.get("flow", []),
        "lead_ids":   data.get("lead_ids", []),
        "stats":      {"sent": 0, "failed": 0, "pending": 0},
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "last_run_at": None,
    }
    result = campaigns_col.insert_one(doc)
    saved  = campaigns_col.find_one({"_id": result.inserted_id})
    return jsonify({"campaign": serialize_campaign(saved)}), 201


@app.route("/api/campaigns/<campaign_id>", methods=["GET"])
@login_required
@owner_required
def api_get_campaign(campaign_id):
    try: oid = ObjectId(campaign_id)
    except InvalidId: return jsonify({"error": "Invalid campaign id"}), 400
    c = campaigns_col.find_one({"_id": oid, "owner_id": current_user_id()})
    if not c: return jsonify({"error": "Campaign not found"}), 404
    return jsonify({"campaign": serialize_campaign(c)})


@app.route("/api/campaigns/<campaign_id>", methods=["PUT", "PATCH"])
@login_required
@owner_required
def api_update_campaign(campaign_id):
    try: oid = ObjectId(campaign_id)
    except InvalidId: return jsonify({"error": "Invalid campaign id"}), 400
    data = request.get_json(silent=True) or {}
    update = {"updated_at": datetime.utcnow()}
    if "name" in data:        update["name"]        = (data["name"] or "").strip()
    if "description" in data: update["description"] = (data["description"] or "").strip()
    if "flow" in data:        update["flow"]        = data["flow"]
    if "lead_ids" in data:    update["lead_ids"]    = data["lead_ids"]
    if "status" in data and data["status"] in ("draft", "active", "paused", "completed"):
        update["status"] = data["status"]
    campaigns_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": update})
    c = campaigns_col.find_one({"_id": oid})
    if not c: return jsonify({"error": "Campaign not found"}), 404
    return jsonify({"campaign": serialize_campaign(c)})


@app.route("/api/campaigns/<campaign_id>", methods=["DELETE"])
@login_required
@owner_required
def api_delete_campaign(campaign_id):
    try: oid = ObjectId(campaign_id)
    except InvalidId: return jsonify({"error": "Invalid campaign id"}), 400
    result = campaigns_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0: return jsonify({"error": "Campaign not found"}), 404
    executions_col.delete_many({"campaign_id": campaign_id})
    return jsonify({"deleted": True})


@app.route("/api/campaigns/<campaign_id>/launch", methods=["POST"])
@login_required
@owner_required
def api_launch_campaign(campaign_id):
    try: ObjectId(campaign_id)
    except InvalidId: return jsonify({"error": "Invalid campaign id"}), 400
    ok = launch_campaign(campaign_id, current_user_id())
    if not ok:
        return jsonify({"error": "Could not launch campaign. Check leads are attached and integrations are configured."}), 400
    return jsonify({
        "launched": True,
        "note": f"Sending in bursts of {CAMPAIGN_BATCH_SIZE} with a {CAMPAIGN_BATCH_WAIT_MIN}-{CAMPAIGN_BATCH_WAIT_MAX}s gap between bursts.",
    })


@app.route("/api/campaigns/<campaign_id>/logs", methods=["GET"])
@login_required
@owner_required
def api_campaign_logs(campaign_id):
    logs = list(executions_col.find(
        {"campaign_id": campaign_id, "owner_id": current_user_id()}
    ).sort("executed_at", -1).limit(500))
    return jsonify({"logs": [serialize_execution(e) for e in logs]})


# ==================================================================
# INTEGRATIONS / CREDENTIALS API  (owner only)
# ==================================================================

@app.route("/api/integrations", methods=["GET"])
@login_required
@owner_required
def api_get_integrations():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    if not user: return jsonify({"error": "User not found"}), 404

    # Self-heal: older accounts created before this feature won't have a token yet
    webhook_token = user.get("webhook_token")
    if not webhook_token:
        webhook_token = generate_webhook_token()
        users_col.update_one({"_id": user["_id"]}, {"$set": {"webhook_token": webhook_token}})

    creds = user.get("integrations", {})
    return jsonify({
        "evo_instance":        creds.get("evo_instance", ""),
        "gmail_address":       creds.get("gmail_address", ""),
        "gmail_app_password":  "●●●●●●●●" if creds.get("gmail_app_password") else "",
        "resend_api_key":      "●●●●●●●●" if creds.get("resend_api_key") else "",
        "resend_from_address": creds.get("resend_from_address", ""),
        "ai_whatsapp_prompt":  creds.get("ai_whatsapp_prompt", ""),
        "ai_email_prompt":     creds.get("ai_email_prompt", ""),
        "has_evo":     bool(creds.get("evo_instance")),
        "has_gmail":   bool(creds.get("gmail_app_password")),
        "has_resend":  bool(creds.get("resend_api_key")),
        "webhook_url": request.host_url.rstrip("/") + "/webhook/" + webhook_token,
    })


@app.route("/api/integrations", methods=["POST"])
@login_required
@owner_required
def api_save_integrations():
    data = request.get_json(silent=True) or {}
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    existing = user.get("integrations", {}) if user else {}

    def maybe_update(key):
        val = data.get(key, "")
        if val and val != "●●●●●●●●":
            existing[key] = val.strip()

    maybe_update("evo_instance")
    maybe_update("gmail_address")
    maybe_update("gmail_app_password")
    maybe_update("resend_api_key")
    maybe_update("resend_from_address")

    if "ai_whatsapp_prompt" in data:
        existing["ai_whatsapp_prompt"] = (data.get("ai_whatsapp_prompt") or "").strip()
    if "ai_email_prompt" in data:
        existing["ai_email_prompt"] = (data.get("ai_email_prompt") or "").strip()

    update = {"integrations": existing}
    if not user.get("webhook_token"):
        update["webhook_token"] = generate_webhook_token()

    users_col.update_one({"_id": ObjectId(current_user_id())}, {"$set": update})
    return jsonify({"saved": True})


@app.route("/api/integrations/test/whatsapp", methods=["POST"])
@login_required
@owner_required
def api_test_whatsapp():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    result = get_instance_status(creds.get("evo_instance", ""))
    return jsonify(result)


@app.route("/api/integrations/test/resend", methods=["POST"])
@login_required
@owner_required
def api_test_resend():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    result = verify_resend_key(creds.get("resend_api_key", ""))
    return jsonify(result)


# ==================================================================
# PROFILE API
# ==================================================================

@app.route("/api/profile", methods=["GET"])
@login_required
def api_get_profile():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    if not user: return jsonify({"error": "User not found"}), 404
    return jsonify({
        "username": user.get("username", ""),
        "email": user.get("email", ""),
        "phone": user.get("phone", ""),
        "business_name": user.get("business_name", ""),
        "business_type": user.get("business_type", ""),
        "website": user.get("website", ""),
        "address": user.get("address", ""),
        "plan": user.get("plan", {}),
        "editable": is_owner(),
    })


@app.route("/api/profile", methods=["POST"])
@login_required
@owner_required
def api_save_profile():
    data = request.get_json(silent=True) or {}
    update = {}
    for field in ("phone", "business_name", "business_type", "website", "address"):
        if field in data:
            update[field] = (data.get(field) or "").strip()
    if "email" in data and data["email"]:
        new_email = data["email"].strip().lower()
        clash = users_col.find_one({"email": new_email, "type": "user", "_id": {"$ne": ObjectId(current_user_id())}})
        if clash:
            return jsonify({"error": "That email is already in use"}), 400
        update["email"] = new_email
    if data.get("new_password"):
        update["password"] = generate_password_hash(data["new_password"])
    users_col.update_one({"_id": ObjectId(current_user_id())}, {"$set": update})
    return jsonify({"saved": True})


# ==================================================================
# TEAM MANAGEMENT API  (owner only)
# ==================================================================

@app.route("/api/team", methods=["GET"])
@login_required
@owner_required
def api_list_team():
    members = list(teams_col.find({"owner_id": current_user_id()}).sort("created_at", -1))
    return jsonify({"members": [serialize_team_member(m) for m in members]})


@app.route("/api/team", methods=["POST"])
@login_required
@owner_required
def api_invite_team_member():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    name  = (data.get("name") or "").strip()
    if not email:
        return jsonify({"error": "Email is required"}), 400
    if teams_col.find_one({"email": email}) or users_col.find_one({"email": email}):
        return jsonify({"error": "That email is already registered"}), 400

    temp_password = generate_temp_password()
    owner = users_col.find_one({"_id": ObjectId(current_user_id())})
    business_name = (owner.get("business_name") if owner else "") or "PravaahAI"

    member_doc = {
        "owner_id": current_user_id(),
        "name": name or email.split("@")[0],
        "email": email,
        "password": generate_password_hash(temp_password),
        "role": "member",
        "status": "active",
        "created_at": datetime.utcnow(),
        "last_login": None,
    }
    result = teams_col.insert_one(member_doc)

    # Send credentials using PravaahAI's own Resend account (not the user's),
    # configured via .env — separate from the per-account Resend integration
    # used for outreach campaigns.
    platform_resend_key  = os.getenv("PLATFORM_RESEND_API_KEY", "")
    platform_from_email  = os.getenv("PLATFORM_RESEND_FROM", "team@pravaahai.app")
    login_url = request.host_url.rstrip("/") + "/login"

    email_body = (
        f"<p>Hi {member_doc['name']},</p>"
        f"<p>You've been added as a team member on <strong>{business_name}</strong>'s PravaahAI account.</p>"
        f"<p><strong>Login email:</strong> {email}<br/>"
        f"<strong>Temporary password:</strong> {temp_password}</p>"
        f"<p>Log in here: <a href=\"{login_url}\">{login_url}</a></p>"
        f"<p>Please change your password after logging in.</p>"
    )

    email_sent = False
    email_error = ""
    if platform_resend_key:
        try:
            send_resend_email(platform_resend_key, platform_from_email, email, "Your PravaahAI team invite", email_body)
            email_sent = True
        except Exception as e:
            email_error = str(e)
    else:
        email_error = "PLATFORM_RESEND_API_KEY not set in .env"

    saved = teams_col.find_one({"_id": result.inserted_id})
    resp = {"member": serialize_team_member(saved), "email_sent": email_sent}
    if not email_sent:
        # Still return the temp password so the owner can share it manually
        resp["temp_password"] = temp_password
        resp["email_error"] = email_error
    return jsonify(resp), 201


@app.route("/api/team/<member_id>", methods=["DELETE"])
@login_required
@owner_required
def api_remove_team_member(member_id):
    try: oid = ObjectId(member_id)
    except InvalidId: return jsonify({"error": "Invalid member id"}), 400
    result = teams_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0:
        return jsonify({"error": "Team member not found"}), 404
    # Unassign their leads so they fall back into the pool
    leads_col.update_many({"owner_id": current_user_id(), "assigned_to": member_id}, {"$set": {"assigned_to": None}})
    return jsonify({"deleted": True})


@app.route("/api/team/<member_id>/status", methods=["PATCH"])
@login_required
@owner_required
def api_toggle_team_member(member_id):
    try: oid = ObjectId(member_id)
    except InvalidId: return jsonify({"error": "Invalid member id"}), 400
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("active", "disabled"):
        return jsonify({"error": "status must be active or disabled"}), 400
    result = teams_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": {"status": status}})
    if result.matched_count == 0:
        return jsonify({"error": "Team member not found"}), 404
    return jsonify({"updated": True})


# ==================================================================
# AI GENERATE (manual button in flow builder)
# ==================================================================

@app.route("/api/ai/generate", methods=["POST"])
@login_required
def api_ai_generate():
    data = request.get_json(silent=True) or {}
    lead_id      = data.get("lead_id")
    content_type = data.get("type")  # "whatsapp" or "email"
    instructions = data.get("instructions", "")

    if content_type not in ("whatsapp", "email"):
        return jsonify({"error": "type must be 'whatsapp' or 'email'"}), 400

    try:
        oid = ObjectId(lead_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid lead id"}), 400

    lead = leads_col.find_one({"_id": oid, "owner_id": current_user_id()})
    if not lead:
        return jsonify({"error": "Lead not found"}), 404

    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    custom_prompt = creds.get("ai_whatsapp_prompt", "") if content_type == "whatsapp" else creds.get("ai_email_prompt", "")

    result = generate_ai_content(lead, content_type, instructions, custom_prompt)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "AI generation failed")}), 400

    return jsonify(result)


# ==================================================================
# TEST CAMPAIGN WHATSAPP STEP
# ==================================================================

@app.route("/api/campaigns/<campaign_id>/test-whatsapp", methods=["POST"])
@login_required
@owner_required
def api_test_campaign_whatsapp(campaign_id):
    data = request.get_json(silent=True) or {}
    step_index = data.get("step_index")
    phone      = (data.get("phone") or "").strip()
    lead_id    = data.get("lead_id")

    try:
        oid = ObjectId(campaign_id)
    except InvalidId:
        return jsonify({"error": "Invalid campaign id"}), 400

    campaign = campaigns_col.find_one({"_id": oid, "owner_id": current_user_id()})
    if not campaign:
        return jsonify({"error": "Campaign not found"}), 404

    flow = campaign.get("flow", [])
    if step_index is None or not isinstance(step_index, int) or not (0 <= step_index < len(flow)):
        return jsonify({"error": "Invalid step index — save the flow first"}), 400

    step = flow[step_index]
    if step.get("type") != "whatsapp":
        return jsonify({"error": "That step is not a WhatsApp step"}), 400

    lead = None
    if lead_id:
        try:
            lead = leads_col.find_one({"_id": ObjectId(lead_id), "owner_id": current_user_id()})
        except InvalidId:
            return jsonify({"error": "Invalid lead id"}), 400
        if not lead:
            return jsonify({"error": "Lead not found"}), 404

    if not lead and not phone:
        return jsonify({"error": "Provide a phone number or select a lead"}), 400

    target_phone = phone or lead.get("phone", "")
    if not target_phone:
        return jsonify({"error": "That lead has no phone number"}), 400

    lead_ctx = lead or {
        "name": "Test Lead", "business_name": "", "email": "",
        "phone": target_phone, "website": "", "description": "",
    }

    user  = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    evo_instance = creds.get("evo_instance", "")
    if not evo_instance:
        return jsonify({"error": "WhatsApp instance not configured in Settings"}), 400

    message = step.get("message", "")
    if step.get("use_ai"):
        ai_prompt  = creds.get("ai_whatsapp_prompt", "")
        ai_result  = generate_ai_content(lead_ctx, "whatsapp", step.get("ai_instructions", ""), ai_prompt)
        if not ai_result.get("success"):
            return jsonify({"error": f"AI generation failed: {ai_result.get('error')}"}), 400
        message = ai_result.get("message", message)

    message = render_template_vars(message, lead_ctx)
    message = f"[TEST] {message}"

    result = send_whatsapp_message(evo_instance, target_phone, message)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "Send failed")}), 400

    return jsonify({"sent": True, "to": target_phone, "message": message})


# ==================================================================
# WEBHOOK — receives inbound WhatsApp messages from Evolution API
# Each user gets a unique URL: /webhook/<their_webhook_token>
# ==================================================================

def _extract_incoming_text(message_obj: dict) -> str:
    if not message_obj:
        return ""
    return (
        message_obj.get("conversation")
        or (message_obj.get("extendedTextMessage") or {}).get("text")
        or (message_obj.get("imageMessage") or {}).get("caption")
        or ""
    ).strip()


def _process_incoming_whatsapp(owner_id: str, phone_raw: str, text: str):
    """Runs in a background thread so the webhook can respond to Evolution
    API instantly. Logs the message, scores the lead, assigns a team member
    on first reply, and sends back an AI-generated reply."""
    try:
        norm_phone = normalize_phone(phone_raw)
        owner = users_col.find_one({"_id": ObjectId(owner_id)})
        if not owner:
            return

        lead = leads_col.find_one({
            "owner_id": owner_id,
            "phone": {"$regex": re.escape(norm_phone[-9:])} if norm_phone else "$^",
        }) if norm_phone else None

        # Log the inbound message even if we can't match it to a lead yet,
        # so nothing is silently lost.
        lead_id = str(lead["_id"]) if lead else ""
        messages_col.insert_one({
            "owner_id": owner_id, "lead_id": lead_id, "direction": "in",
            "channel": "whatsapp", "text": text, "created_at": datetime.utcnow(),
        })

        if not lead:
            return  # unknown number — nothing further we can automate safely

        history = list(messages_col.find({"owner_id": owner_id, "lead_id": lead_id}).sort("created_at", 1))

        # 1. Score the lead's temperature based on the conversation so far
        new_status = classify_lead_temperature(lead, text, history, lead.get("status", "cold"))

        # 2. Assign to a team member on first-ever reply, round robin
        update = {"status": new_status, "updated_at": datetime.utcnow()}
        if not lead.get("assigned_to"):
            assigned = assign_round_robin(owner_id)
            if assigned:
                update["assigned_to"] = assigned
        leads_col.update_one({"_id": lead["_id"]}, {"$set": update})

        # 3. Generate and send an AI auto-reply
        creds = owner.get("integrations", {})
        evo_instance = creds.get("evo_instance", "")
        if not evo_instance:
            return
        reply = generate_chat_reply(
            lead, text, history,
            task_prompt=lead.get("ai_task_prompt", ""),
            system_prompt=creds.get("ai_whatsapp_prompt", ""),
        )
        if reply.get("success") and reply.get("message"):
            send_result = send_whatsapp_message(evo_instance, lead.get("phone", phone_raw), reply["message"])
            if send_result.get("success"):
                messages_col.insert_one({
                    "owner_id": owner_id, "lead_id": lead_id, "direction": "out",
                    "channel": "whatsapp", "text": reply["message"], "created_at": datetime.utcnow(),
                })
    except Exception:
        # Never let a background webhook failure crash the app
        pass


@app.route("/webhook/<token>", methods=["POST"])
def webhook_receive(token):
    user = users_col.find_one({"webhook_token": token, "type": "user"})
    if not user:
        return jsonify({"error": "Invalid webhook token"}), 404

    payload = request.get_json(silent=True) or {}
    data = payload.get("data", payload) or {}
    key = data.get("key", {}) or {}

    if key.get("fromMe"):
        return jsonify({"ok": True}), 200  # ignore our own outgoing messages echoed back

    remote_jid = key.get("remoteJid", "") or data.get("remoteJid", "")
    phone = remote_jid.split("@")[0] if remote_jid else ""
    text = _extract_incoming_text(data.get("message", {}))

    if not phone or not text:
        return jsonify({"ok": True}), 200

    owner_id = str(user["_id"])
    threading.Thread(target=_process_incoming_whatsapp, args=(owner_id, phone, text), daemon=True).start()
    return jsonify({"received": True}), 200


# ==================================================================
# DASHBOARD STATS API
# ==================================================================

@app.route("/api/dashboard/stats", methods=["GET"])
@login_required
def api_dashboard_stats():
    owner_id = current_user_id()
    leads_query = _leads_scope_query()

    total_leads = leads_col.count_documents(leads_query)
    week_ago = datetime.utcnow() - timedelta(days=7)
    new_this_week = leads_col.count_documents({**leads_query, "created_at": {"$gte": week_ago}})

    status_counts = {"cold": 0, "warm": 0, "hot": 0}
    for s in LEAD_STATUSES:
        status_counts[s] = leads_col.count_documents({**leads_query, "status": s})

    exec_query = {"owner_id": owner_id}
    if not is_owner():
        my_lead_ids = [str(l["_id"]) for l in leads_col.find(leads_query, {"_id": 1})]
        exec_query["lead_id"] = {"$in": my_lead_ids}

    messages_sent = executions_col.count_documents({**exec_query, "status": "sent"})
    messages_failed = executions_col.count_documents({**exec_query, "status": "failed"})

    # Last 14 days sent-message timeseries for the chart
    since = datetime.utcnow() - timedelta(days=14)
    daily = {}
    for e in executions_col.find({**exec_query, "status": "sent", "executed_at": {"$gte": since}}, {"executed_at": 1}):
        day = e["executed_at"].strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0) + 1
    timeseries = []
    for i in range(13, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        timeseries.append({"date": day, "sent": daily.get(day, 0)})

    with_email = leads_col.count_documents({**leads_query, "email": {"$nin": ["", None]}})
    with_website = leads_col.count_documents({**leads_query, "website": {"$nin": ["", None]}})

    return jsonify({
        "total_leads": total_leads,
        "new_this_week": new_this_week,
        "with_email": with_email,
        "with_website": with_website,
        "messages_sent": messages_sent,
        "messages_failed": messages_failed,
        "status_counts": status_counts,
        "timeseries": timeseries,
    })


# ==================================================================
# RUN
# ==================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)