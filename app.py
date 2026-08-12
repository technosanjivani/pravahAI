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
import json

import pandas as pd

# Sender modules
from evo import send_whatsapp_message, get_instance_status
from gmail import send_gmail
from resend import send_resend_email, verify_resend_key
import requests

from scraper import scrape_website, normalize_website_for_dedupe

import cloudinary
import cloudinary.uploader

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
)

SUPABASE_URL          = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY  = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_PDF_BUCKET   = os.getenv("SUPABASE_PDF_BUCKET", "property-docs")

BUSINESS_CATEGORIES = {"real_estate", "share_market", "import_export"}
SITE_VISIT_STATUSES = {"new", "read", "confirmed", "done", "won", "lost", "rescheduled"}


def upload_media_to_cloudinary(file_storage, resource_type="auto"):
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
        return {"success": False, "error": "Cloudinary is not configured on the server"}
    try:
        result = cloudinary.uploader.upload(file_storage, resource_type=resource_type, folder="pravaah-inventory")
        return {"success": True, "url": result.get("secure_url", ""), "resource_type": result.get("resource_type", "")}
    except Exception as e:
        return {"success": False, "error": str(e)}


def upload_pdf_to_supabase(file_storage, owner_id):
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return {"success": False, "error": "Supabase is not configured on the server"}
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", file_storage.filename or "document.pdf")
    filename = f"{owner_id}/{secrets.token_urlsafe(8)}_{safe_name}"
    url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{SUPABASE_PDF_BUCKET}/{filename}"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": file_storage.mimetype or "application/pdf",
                "x-upsert": "true",
            },
            data=file_storage.read(),
            timeout=30,
        )
        resp.raise_for_status()
        public_url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{SUPABASE_PDF_BUCKET}/{filename}"
        return {"success": True, "url": public_url}
    except Exception as e:
        return {"success": False, "error": str(e)}


def generate_view_id():
    return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]


def serialize_inventory(inv):
    return {
        "_id": str(inv["_id"]),
        "view_id": inv.get("view_id", ""),
        "headline": inv.get("headline", ""),
        "description": inv.get("description", ""),
        "price": inv.get("price", ""),
        "city": inv.get("city", ""),
        "area": inv.get("area", ""),
        "features": inv.get("features", []),
        "images": inv.get("images", []),
        "videos": inv.get("videos", []),
        "pdf_url": inv.get("pdf_url", ""),
        "status": inv.get("status", "active"),
        "view_url": public_https_url(f"/view/{inv.get('view_id','')}"),
        "created_at": inv.get("created_at").isoformat() if inv.get("created_at") else None,
        "updated_at": inv.get("updated_at").isoformat() if inv.get("updated_at") else None,
    }


def serialize_site_visit(v):
    return {
        "_id": str(v["_id"]),
        "lead_id": v.get("lead_id", ""),
        "lead_name": v.get("lead_name", ""),
        "lead_phone": v.get("lead_phone", ""),
        "budget": v.get("budget", ""),
        "preferred_location": v.get("preferred_location", ""),
        "property_id": v.get("property_id", ""),
        "property_headline": v.get("property_headline", ""),
        "view_url": v.get("view_url", ""),
        "visit_date": v.get("visit_date", ""),
        "visit_time": v.get("visit_time", ""),
        "status": v.get("status", "new"),
        "created_at": v.get("created_at").isoformat() if v.get("created_at") else None,
    }


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
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

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
plans_col      = db["pravah-plans"]        # subscription plans (India / International)
pricing_col    = db["pravah-eva-pricing"]  # global Eva per-minute pricing settings
segments_col   = db["pravah-segments"]
meeting_templates_col = db["pravah-meeting-templates"]  # per-owner availability/config
meetings_col          = db["pravah-meetings"]            # booked meetings
webhook_logs_col      = db["pravah-webhook-logs"]         # raw inbound/outbound webhook events, for debugging

webhook_logs_col      = db["pravah-webhook-logs"]         # raw inbound/outbound webhook events, for debugging
inventory_col         = db["pravah-inventory"]             # real-estate property listings
site_visits_col       = db["pravah-site-visits"]
# ----------------------------------
# Create Indexes
# ----------------------------------

try:
    users_col.create_index("username", unique=True)
    users_col.create_index("email", unique=True)
    users_col.create_index("webhook_token", unique=True, sparse=True)
    users_col.create_index("wirebase_webhook_token", unique=True, sparse=True)
    leads_col.create_index("owner_id")
    leads_col.create_index([("owner_id", 1), ("created_at", -1)])
    leads_col.create_index([("owner_id", 1), ("phone", 1)])
    campaigns_col.create_index("owner_id")
    executions_col.create_index([("campaign_id", 1), ("lead_id", 1)])
    executions_col.create_index("owner_id")
    messages_col.create_index([("owner_id", 1), ("lead_id", 1), ("created_at", -1)])
    teams_col.create_index("owner_id")
    teams_col.create_index("email", unique=True)
    plans_col.create_index("region")
    pricing_col.create_index("key", unique=True)
    segments_col.create_index([("owner_id", 1), ("name", 1)], unique=True)
    leads_col.create_index([("owner_id", 1), ("segment_id", 1)])
    widgets_col = db["pravah-web-widgets"]
    widgets_col.create_index("owner_id")
    widgets_col.create_index("public_id", unique=True)
    meeting_templates_col.create_index("owner_id", unique=True)
    meetings_col.create_index("owner_id")
    meetings_col.create_index([("owner_id", 1), ("scheduled_at", 1)])
    meetings_col.create_index([("status", 1), ("reminder_15_sent", 1), ("reminder_5_sent", 1), ("scheduled_at", 1)])
    meetings_col.create_index("lead_id")
    webhook_logs_col.create_index([("owner_id", 1), ("created_at", -1)])
    inventory_col.create_index("owner_id")
    inventory_col.create_index("view_id", unique=True)
    inventory_col.create_index([("owner_id", 1), ("city", 1)])
    site_visits_col.create_index("owner_id")
    site_visits_col.create_index([("owner_id", 1), ("status", 1)])
except Exception:
    pass


def seed_admin_defaults():
    """Creates the global Eva pricing document the first time the app boots."""
    if pricing_col.count_documents({"key": "global"}) == 0:
        pricing_col.insert_one({
            "key": "global",
            "india_price_per_min": 2.0,            # INR
            "international_price_per_min": 0.05,   # USD
            "updated_at": datetime.utcnow(),
        })


seed_admin_defaults()

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

LEAD_STATUSES = {"pending", "cold", "warm", "hot"}

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

# ----------------------------------
# Lead website scraper — job tracking
# ----------------------------------
_scrape_jobs = {}                  # job_id -> job state dict (in-memory, like the AI throttle map above)
_scrape_jobs_lock = threading.Lock()


def _new_scrape_job(owner_id, total):
    job_id = secrets.token_urlsafe(12)
    with _scrape_jobs_lock:
        _scrape_jobs[job_id] = {
            "owner_id": owner_id,
            "total": total,
            "processed": 0,
            "found_email": 0,
            "found_about": 0,
            "failed": 0,
            "current_lead_name": "",
            "status": "running",       # running | completed
            "started_at": datetime.utcnow().isoformat(),
            "log": [],                 # last 40 events, newest last
        }
    return job_id


def _push_scrape_log(job_id, entry):
    with _scrape_jobs_lock:
        job = _scrape_jobs.get(job_id)
        if not job:
            return
        job["log"].append(entry)
        job["log"] = job["log"][-40:]


def run_lead_scrape_job(job_id, owner_id, lead_ids):
    """Background worker: visits each lead's website, and — only where the
    field is currently empty — fills in email and/or description (About text).
    Existing values are never overwritten."""
    for lead_id in lead_ids:
        try:
            oid = ObjectId(lead_id)
        except InvalidId:
            continue
        lead = leads_col.find_one({"_id": oid, "owner_id": owner_id})
        if not lead:
            continue

        with _scrape_jobs_lock:
            _scrape_jobs[job_id]["current_lead_name"] = lead.get("name", "") or lead.get("business_name", "")

        website = lead.get("website", "")
        lead_name = lead.get("name", "") or lead.get("business_name", "") or "Lead"

        if not website:
            with _scrape_jobs_lock:
                j = _scrape_jobs[job_id]
                j["processed"] += 1
                j["failed"] += 1
            _push_scrape_log(job_id, {"name": lead_name, "result": "skipped", "reason": "No website on file"})
            continue

        try:
            data = scrape_website(website)
        except Exception as e:
            with _scrape_jobs_lock:
                j = _scrape_jobs[job_id]
                j["processed"] += 1
                j["failed"] += 1
            _push_scrape_log(job_id, {"name": lead_name, "result": "failed", "reason": str(e)[:120]})
            continue

        got_email = bool(data.get("email")) and not lead.get("email")
        got_about = bool(data.get("about_text"))

        update = {"updated_at": datetime.utcnow()}
        if got_email:
            update["email"] = data["email"].split(",")[0].strip().lower()
        if got_about:
            update["description"] = data["about_text"][:2000]
        if len(update) > 1:
            leads_col.update_one({"_id": oid}, {"$set": update})

        with _scrape_jobs_lock:
            j = _scrape_jobs[job_id]
            j["processed"] += 1
            if got_email:
                j["found_email"] += 1
            if got_about:
                j["found_about"] += 1
            if not got_email and not got_about:
                j["failed"] += 1

        _push_scrape_log(job_id, {
            "name": lead_name,
            "result": "found" if (got_email or got_about) else "empty",
            "email": got_email,
            "about": got_about,
        })

    with _scrape_jobs_lock:
        _scrape_jobs[job_id]["status"] = "completed"
        _scrape_jobs[job_id]["current_lead_name"] = ""


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


def public_https_url(path: str = "") -> str:
    """Builds an absolute HTTPS URL for this host, regardless of what scheme
    the request actually arrived on (e.g. behind an http-only proxy/load
    balancer). Wirebase/Evolution webhook URLs must always be https://."""
    host = request.host  # includes port if non-standard, no scheme
    return f"https://{host}{path}"


_REDACT_HEADERS = {"x-webhook-secret", "x-api-key", "authorization", "x-eva-secret", "cookie"}

def safe_request_headers():
    out = {}
    for k, v in request.headers.items():
        out[k] = "●●●●●●●●" if k.lower() in _REDACT_HEADERS else v
    return out


WEBHOOK_LOG_MAX_PER_OWNER = 300

def log_webhook_event(owner_id, source, direction, status, note="", payload=None, headers=None, response=None):
    """Stores one raw webhook/send event so the WhatsApp Bot dashboard can
    show exactly what came in and what went out — request payload, headers,
    our response/decision, and any error — for debugging Wirebase/Evolution."""
    try:
        webhook_logs_col.insert_one({
            "owner_id": owner_id or "",
            "source": source,        # "wirebase" | "evolution" | "eva"
            "direction": direction,  # "inbound" | "outbound"
            "status": status,        # "received" | "skipped" | "sent" | "error" | "info"
            "note": note or "",
            "payload": payload if payload is not None else {},
            "headers": headers or {},
            "response": response if response is not None else {},
            "created_at": datetime.utcnow(),
        })
        if owner_id:
            count = webhook_logs_col.count_documents({"owner_id": owner_id})
            if count > WEBHOOK_LOG_MAX_PER_OWNER:
                excess = count - WEBHOOK_LOG_MAX_PER_OWNER
                old_ids = [d["_id"] for d in webhook_logs_col.find(
                    {"owner_id": owner_id}, {"_id": 1}
                ).sort("created_at", 1).limit(excess)]
                if old_ids:
                    webhook_logs_col.delete_many({"_id": {"$in": old_ids}})
    except Exception as e:
        print(f"[WEBHOOK-LOG] failed to store event: {e}", flush=True)


def serialize_webhook_log(d):
    return {
        "_id": str(d["_id"]),
        "source": d.get("source", ""),
        "direction": d.get("direction", ""),
        "status": d.get("status", ""),
        "note": d.get("note", ""),
        "payload": d.get("payload", {}),
        "headers": d.get("headers", {}),
        "response": d.get("response", {}),
        "created_at": d.get("created_at").isoformat() if d.get("created_at") else None,
    }



def generate_temp_password(length=10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def parse_iso_utc(s):
    """Parses an ISO datetime string (with or without trailing Z / ms) into
    a naive UTC datetime, matching how the rest of this file stores time."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


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


def admin_required(view):
    """Restricts a route to platform admins (account_type == 'admin')."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect("/login")
        if session.get("account_type") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admin access only"}), 403
            return redirect("/dashboard")
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

def generate_public_widget_id():
    return "wgt_" + secrets.token_urlsafe(16)


def build_widget_embed_snippet(public_id, color):
    eva_public_base = os.getenv("EVA_PUBLIC_BASE_URL", os.getenv("EVA_API_BASE_URL", "")).rstrip("/")
    return (
        f'<script src="{eva_public_base}/embed/widget.js" '
        f'data-public-id="{public_id}" data-color="{color}" async></script>'
    )


def serialize_widget(w):
    return {
        "_id": str(w["_id"]),
        "public_id": w.get("public_id", ""),
        "name": w.get("name", ""),
        "agent_id": w.get("agent_id", ""),
        "status": w.get("status", "active"),
        "primary_color": w.get("primary_color", "#2454E8"),
        "greeting": w.get("greeting", "Hi! How can I help you today?"),
        "collect_lead": w.get("collect_lead", True),
        "require_lead_before_chat": w.get("require_lead_before_chat", True),
        "created_at": w.get("created_at").isoformat() if w.get("created_at") else None,
        "embed_snippet": build_widget_embed_snippet(w.get("public_id", ""), w.get("primary_color", "#2454E8")),
    }

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
        "status": lead.get("status", "pending"),
        "segment_id": lead.get("segment_id", ""),
        "assigned_to": lead.get("assigned_to"),
        "ai_task_prompt": lead.get("ai_task_prompt", ""),
        "ai_disabled": bool(lead.get("ai_disabled", False)),
        "created_at": lead.get("created_at").isoformat() if lead.get("created_at") else None,
        "updated_at": lead.get("updated_at").isoformat() if lead.get("updated_at") else None,
        "ai_score": lead.get("ai_score", 0),
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
        "segment_id": (data.get("segment_id") or "").strip(),
    }


def serialize_campaign(c):
    return {
        "_id": str(c["_id"]),
        "name": c.get("name", ""),
        "description": c.get("description", ""),
        "status": c.get("status", "draft"),
        "nodes": c.get("nodes", []),
        "edges": c.get("edges", []),
        "lead_ids": c.get("lead_ids", []),
        "schedule_type": c.get("schedule_type", "now"),
        "scheduled_at": c.get("scheduled_at").isoformat() if c.get("scheduled_at") else None,
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


def serialize_segment(s):
    return {
        "_id": str(s["_id"]),
        "name": s.get("name", ""),
        "lead_count": leads_col.count_documents({"owner_id": s["owner_id"], "segment_id": str(s["_id"])}),
        "created_at": s.get("created_at").isoformat() if s.get("created_at") else None,
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


def serialize_plan(p):
    return {
        "_id": str(p["_id"]),
        "name": p.get("name", ""),
        "region": p.get("region", "international"),
        "currency": p.get("currency", "USD"),
        "price": p.get("price", 0),
        "billing_cycle": p.get("billing_cycle", "monthly"),
        "eva_minutes_included": p.get("eva_minutes_included", 0),
        "lead_limit": p.get("lead_limit", 0),
        "features": p.get("features", []),
        "created_at": p.get("created_at").isoformat() if p.get("created_at") else None,
    }


def serialize_admin_user(u):
    plan = None
    if u.get("plan_id"):
        try:
            plan = plans_col.find_one({"_id": ObjectId(u["plan_id"])})
        except InvalidId:
            plan = None
    return {
        "_id": str(u["_id"]),
        "username": u.get("username", ""),
        "email": u.get("email", ""),
        "phone": u.get("phone", ""),
        "business_name": u.get("business_name", ""),
        "status": u.get("status", "active"),
        "email_verified": u.get("email_verified", False),
        "account_type": u.get("account_type", "user"),
        "region": u.get("region", "international"),
        "plan": {"_id": str(plan["_id"]), "name": plan.get("name", "")} if plan else None,
        "eva_minutes": float(u.get("eva_minutes", 0) or 0),
        "eva_minutes_used": float(u.get("eva_minutes_used", 0) or 0),
        "team_count": teams_col.count_documents({"owner_id": str(u["_id"])}),
        "lead_count": leads_col.count_documents({"owner_id": str(u["_id"])}),
        "created_at": u.get("created_at").isoformat() if u.get("created_at") else None,
        "last_login": u.get("last_login").isoformat() if u.get("last_login") else None,
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
    conversation should be steered), the account's general WA style prompt,
    and up to the last 10 messages of real conversation history so the AI
    doesn't re-introduce itself mid-thread."""
    lead_context = (
        f"Name: {lead.get('name','')}\n"
        f"Business Name: {lead.get('business_name','')}\n"
        f"Email: {lead.get('email','')}\n"
        f"Phone: {lead.get('phone','')}\n"
        f"Website: {lead.get('website','')}\n"
        f"Description: {lead.get('description','')}\n"
    )

    recent_history = history[-10:]
    history_text = "\n".join(
        f"{'Lead' if h.get('direction')=='in' else 'You'}: {h.get('text','')}"
        for h in recent_history
    )

    base_style = (system_prompt or "").strip() or (
        "You are a friendly, concise WhatsApp sales assistant replying to an inbound lead message."
    )
    task = (task_prompt or "").strip() or "Keep the lead engaged and move the conversation toward a sale or a booked call."

    continuity_note = (
        "You are already mid-conversation with this lead — do NOT say 'Hi', 'Hello', "
        "introduce yourself, or restate who you are again. Reply as a natural continuation "
        "of the thread above, referencing what was already discussed where relevant."
        if recent_history else
        "This is the very first message in this conversation, so a brief, natural greeting is fine."
    )

    system = (
        f"{base_style}\n\n"
        f"Your specific goal for this lead: {task}\n\n"
        f"{continuity_note}\n\n"
        "Reply in 1-3 short sentences like a real person texting on WhatsApp. "
        "No signatures, no placeholders. Return ONLY the reply text."
    )
    user_prompt = (
        f"Lead data:\n{lead_context}\n"
        f"Recent conversation (last {len(recent_history)} messages):\n{history_text}\n\n"
        f"Lead's latest message: {incoming_message}\n\n"
        "Write your reply now."
    )

    result = _mistral_chat(system, user_prompt, force_json=False)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "AI reply generation failed")}
    return {"success": True, "message": result["text"]}



def real_estate_extract(lead, incoming_message, history, known_budget="", known_location=""):
    """One AI call that both figures out the next action and drafts the reply,
    for a real-estate owner's WhatsApp bot."""
    history_text = "\n".join(
        f"{'Lead' if h.get('direction')=='in' else 'You'}: {h.get('text','')}"
        for h in history[-10:]
    )
    system = (
        "You are a real-estate sales assistant chatting with a lead over WhatsApp. "
        "Figure out the next best action and return ONLY valid JSON, no markdown fences, "
        "in this exact shape: "
        '{"budget": "extracted budget or empty string", '
        '"preferred_location": "extracted city/area or empty string", '
        '"ready_for_recommendations": true or false — true once you know BOTH budget and location and the lead has not yet been shown options, '
        '"wants_to_book_visit": true or false — true only if the lead has picked a property AND given a date/time to visit, '
        '"chosen_property_hint": "any property name/number the lead referenced, else empty", '
        '"visit_date": "date the lead gave for a visit, else empty", '
        '"visit_time": "time the lead gave for a visit, else empty", '
        '"reply": "your natural 1-3 sentence WhatsApp reply, warm and concise"}'
    )
    user_prompt = (
        f"Lead: {lead.get('name','')}\n"
        f"Known so far — budget: {known_budget or '(not yet known)'}, location: {known_location or '(not yet known)'}\n\n"
        f"Recent conversation:\n{history_text}\n\nLead's latest message: {incoming_message}\n\n"
        "If budget or location is still missing, your reply should politely ask for whichever is missing "
        "(ask for both together if neither is known yet). Never invent property details yourself — "
        "the app will attach real listings separately."
    )
    result = _mistral_chat(system, user_prompt, force_json=True)
    if not result.get("success"):
        return None
    return result["data"]


def find_matching_properties(owner_id, budget, location, limit=3):
    query = {"owner_id": owner_id, "status": "active"}
    if location:
        query["$or"] = [
            {"city": {"$regex": re.escape(location), "$options": "i"}},
            {"area": {"$regex": re.escape(location), "$options": "i"}},
        ]
    return list(inventory_col.find(query).limit(limit))


def real_estate_chat_reply(owner_id, lead, incoming_message, history):
    """Real-estate-specific replacement for generate_chat_reply(): asks for
    budget+location, matches inventory, sends view links, then books a site visit."""
    known_budget = lead.get("re_budget", "")
    known_location = lead.get("re_location", "")
    extracted = real_estate_extract(lead, incoming_message, history, known_budget, known_location)
    if not extracted:
        return {"success": False, "error": "AI extraction failed"}

    budget = (extracted.get("budget") or known_budget or "").strip()
    location = (extracted.get("preferred_location") or known_location or "").strip()
    slot_update = {}
    if budget and budget != known_budget: slot_update["re_budget"] = budget
    if location and location != known_location: slot_update["re_location"] = location
    if slot_update:
        leads_col.update_one({"_id": lead["_id"]}, {"$set": slot_update})

    reply_text = (extracted.get("reply") or "").strip()

    # Lead picked a property + gave a date/time -> book the site visit
    if extracted.get("wants_to_book_visit") and (extracted.get("visit_date") or extracted.get("visit_time")):
        matched = find_matching_properties(owner_id, budget, location, limit=5)
        chosen = None
        hint = (extracted.get("chosen_property_hint") or "").strip().lower()
        if hint:
            for m in matched:
                if hint in (m.get("headline", "") or "").lower():
                    chosen = m
                    break
        if not chosen and matched:
            chosen = matched[0]

        site_visits_col.insert_one({
            "owner_id": owner_id,
            "lead_id": str(lead["_id"]), "lead_name": lead.get("name", ""), "lead_phone": lead.get("phone", ""),
            "budget": budget, "preferred_location": location,
            "property_id": str(chosen["_id"]) if chosen else "",
            "property_headline": chosen.get("headline", "") if chosen else "",
            "view_url": public_https_url(f"/view/{chosen.get('view_id','')}") if chosen else "",
            "visit_date": extracted.get("visit_date", ""), "visit_time": extracted.get("visit_time", ""),
            "status": "new", "created_at": datetime.utcnow(),
        })
        reply_text = (reply_text + " " if reply_text else "") + \
            "Great — our team will call and confirm your site visit shortly! 🏠"
        return {"success": True, "message": reply_text.strip()}

    # Have both budget & location -> show matching listings
    if extracted.get("ready_for_recommendations") and budget and location:
        matched = find_matching_properties(owner_id, budget, location, limit=3)
        if matched:
            lines = [reply_text, "", "Here are a few options for you:"]
            for m in matched:
                view_url = public_https_url(f"/view/{m.get('view_id','')}")
                lines.append(f"🏠 {m.get('headline','')} — {m.get('price','')} ({m.get('city','')}) {view_url}")
            lines.append("")
            lines.append("Let me know which one you'd like to visit, and your preferred date & time!")
            return {"success": True, "message": "\n".join(l for l in lines if l is not None)}
        return {"success": True, "message": (reply_text + " " if reply_text else "") +
                "I couldn't find a perfect match right now — let me check with the team and get back to you."}

    return {"success": True, "message": reply_text or "Could you share your budget and preferred location?"}

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
# Meeting scheduling helpers
# ----------------------------------

MEETING_DAYS = {"monday","tuesday","wednesday","thursday","friday","saturday","sunday"}
MEETING_STATUSES = {"scheduled", "completed", "missed", "rescheduled", "cancelled"}


def serialize_meeting_template(t):
    return {
        "_id": str(t["_id"]),
        "duration_minutes": t.get("duration_minutes", 30),
        "meet_link": t.get("meet_link", ""),
        "admin_whatsapp": t.get("admin_whatsapp", ""),
        "slots": t.get("slots", []),
        "updated_at": t.get("updated_at").isoformat() if t.get("updated_at") else None,
    }


def serialize_meeting(m):
    return {
        "_id": str(m["_id"]),
        "owner_id": m.get("owner_id", ""),
        "lead_id": m.get("lead_id", ""),
        "lead_name": m.get("lead_name", ""),
        "lead_phone": m.get("lead_phone", ""),
        "call_id": m.get("call_id", ""),
        "agent_id": m.get("agent_id", ""),
        "scheduled_at": m.get("scheduled_at").isoformat() if m.get("scheduled_at") else None,
        "duration_minutes": m.get("duration_minutes", 30),
        "meet_link": m.get("meet_link", ""),
        "status": m.get("status", "scheduled"),
        "reminder_15_sent": m.get("reminder_15_sent", False),
        "reminder_5_sent": m.get("reminder_5_sent", False),
        "rescheduled_from": m.get("rescheduled_from", ""),
        "created_at": m.get("created_at").isoformat() if m.get("created_at") else None,
    }


def _meeting_overlaps(owner_id, start_dt, duration_minutes, exclude_id=None):
    """True if an existing *scheduled* meeting for this owner overlaps the window."""
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    window_start = start_dt - timedelta(hours=6)
    window_end = start_dt + timedelta(hours=6)
    query = {
        "owner_id": owner_id, "status": "scheduled",
        "scheduled_at": {"$gte": window_start, "$lte": window_end},
    }
    if exclude_id:
        query["_id"] = {"$ne": exclude_id}
    for m in meetings_col.find(query):
        existing_start = m["scheduled_at"]
        existing_end = existing_start + timedelta(minutes=m.get("duration_minutes", 30))
        if existing_start < end_dt and start_dt < existing_end:
            return True
    return False


def is_time_available(owner_id: str, dt: datetime, duration_minutes: int = None):
    """Checks a requested datetime (naive UTC, matching how this app stores time
    everywhere else) against the owner's weekly availability template and any
    existing bookings. Returns (ok, reason, template)."""
    template = meeting_templates_col.find_one({"owner_id": owner_id})
    if not template:
        return False, "No meeting availability has been configured yet", None
    duration_minutes = duration_minutes or template.get("duration_minutes", 30)
    weekday = dt.strftime("%A").lower()
    hhmm = dt.strftime("%H:%M")
    slot_ok = any(
        s.get("day") == weekday and s.get("available", True)
        and s.get("start", "00:00") <= hhmm <= s.get("end", "00:00")
        for s in template.get("slots", [])
    )
    if not slot_ok:
        return False, "That time is outside the available hours", template
    if _meeting_overlaps(owner_id, dt, duration_minutes):
        return False, "That slot is already booked", template
    return True, "", template


def suggest_alt_slots(owner_id: str, around_dt: datetime, duration_minutes: int, limit: int = 3):
    """Best-effort: scans forward in 30-min steps (up to 7 days) within
    configured hours and returns up to `limit` free slots — useful for
    offering alternatives when a requested time isn't available."""
    template = meeting_templates_col.find_one({"owner_id": owner_id})
    if not template:
        return []
    found = []
    probe = around_dt.replace(minute=(around_dt.minute // 30) * 30, second=0, microsecond=0)
    for _ in range(7 * 48):
        probe += timedelta(minutes=30)
        ok, _, _ = is_time_available(owner_id, probe, duration_minutes)
        if ok:
            found.append(probe)
            if len(found) >= limit:
                break
    return found


def send_meeting_whatsapp(owner, to_phone, text):
    if not owner or not to_phone or not text:
        return {"success": False, "error": "Missing owner/phone/text"}
    return send_whatsapp_dispatch(owner, to_phone, text)


def book_meeting(owner_id, lead, scheduled_at, duration_minutes=None, call_id="", agent_id=""):
    """Core booking routine — shared by the Eva booking webhook and any future
    manual booking path. `lead` needs at least name/phone (and optionally _id
    or lead_id). Returns (success: bool, payload: dict)."""
    ok, reason, template = is_time_available(owner_id, scheduled_at, duration_minutes)
    if not ok:
        alts = suggest_alt_slots(
            owner_id, scheduled_at,
            duration_minutes or (template.get("duration_minutes", 30) if template else 30),
        )
        return False, {"error": reason, "alternatives": [a.isoformat() for a in alts]}

    duration_minutes = duration_minutes or template.get("duration_minutes", 30)
    doc = {
        "owner_id": owner_id,
        "lead_id": str(lead.get("_id", "")) if lead.get("_id") else lead.get("lead_id", ""),
        "lead_name": lead.get("name", ""),
        "lead_phone": lead.get("phone", ""),
        "call_id": call_id,
        "agent_id": agent_id,
        "scheduled_at": scheduled_at,
        "duration_minutes": duration_minutes,
        "meet_link": template.get("meet_link", ""),
        "status": "scheduled",
        "reminder_15_sent": False,
        "reminder_5_sent": False,
        "created_at": datetime.utcnow(),
    }
    inserted = meetings_col.insert_one(doc)
    meeting = meetings_col.find_one({"_id": inserted.inserted_id})

    owner = users_col.find_one({"_id": ObjectId(owner_id)})
    when_str = scheduled_at.strftime("%A, %d %b %Y at %H:%M UTC")
    lead_msg = (
        f"Your meeting is confirmed for {when_str}."
        + (f" Join here: {template.get('meet_link')}" if template.get("meet_link") else "")
    )
    admin_msg = f"📅 New meeting booked with {lead.get('name','a lead')} ({lead.get('phone','')}) for {when_str}."

    if owner and lead.get("phone"):
        send_meeting_whatsapp(owner, lead["phone"], lead_msg)
    if owner and template.get("admin_whatsapp"):
        send_meeting_whatsapp(owner, template["admin_whatsapp"], admin_msg)

    return True, {"meeting": serialize_meeting(meeting)}


def get_meeting_context(owner_id):
    """Builds a plain-English availability summary for Eva's agent prompt.
    Returns None if the owner hasn't configured a meeting template yet."""
    template = meeting_templates_col.find_one({"owner_id": owner_id})
    if not template:
        return None
    by_day = {}
    for s in template.get("slots", []):
        if s.get("available", True):
            by_day.setdefault(s["day"], []).append(f"{s['start']}-{s['end']}")
    if not by_day:
        return None
    lines = [f"{day.capitalize()}: {', '.join(times)}" for day, times in by_day.items()]
    return {
        "duration_minutes": template.get("duration_minutes", 30),
        "availability_text": "; ".join(lines) + " (times in UTC)",
        "booking_webhook_url": f"{PRAVAAH_PUBLIC_BASE_URL}/api/eva-webhook/book-meeting",
    }


# ----------------------------------
# Wirebase sender + provider dispatch
# ----------------------------------

# ----------------------------------
# Wirebase sender + provider dispatch
# ----------------------------------

def send_whatsapp_via_wirebase(base_url: str, api_key: str, instance_name: str, phone: str, message: str) -> dict:
    if not (base_url and api_key and instance_name):
        return {"success": False, "error": "Wirebase is not fully configured in Settings"}
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/public/send",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"instanceName": instance_name, "to": phone, "type": "text", "message": message},
            timeout=10,
        )
        status_code = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:500]
        resp.raise_for_status()
        return {"success": True, "raw": body, "status_code": status_code}
    except requests.exceptions.RequestException as e:
        status_code = e.response.status_code if getattr(e, "response", None) is not None else None
        try:
            body = e.response.json() if getattr(e, "response", None) is not None else None
        except ValueError:
            body = e.response.text[:500] if getattr(e, "response", None) is not None else None
        return {"success": False, "error": str(e), "status_code": status_code, "raw": body}

def send_whatsapp_dispatch(user: dict, phone: str, message: str) -> dict:
    """Single entry point for every outbound WhatsApp send in the app.
    Routes through Evolution API or Wirebase depending on the user's
    integrations.active_provider toggle."""
    creds = user.get("integrations", {}) or {}
    provider = creds.get("active_provider", "evo")

    wirebase_ready = bool(
        creds.get("wirebase_base_url") and creds.get("wirebase_api_key") and creds.get("wirebase_instance_name")
    )
    evo_ready = bool(creds.get("evo_instance"))

    # Safety net: if the toggle says "evo" but evo isn't configured while
    # Wirebase is fully configured, use Wirebase instead of failing silently.
    if provider != "wirebase" and wirebase_ready and not evo_ready:
        log("WA-DISPATCH", f"active_provider={provider!r} but only Wirebase is configured — using Wirebase instead")
        provider = "wirebase"

    if provider == "wirebase":
        if not wirebase_ready:
            return {"success": False, "error": "Wirebase is not fully configured in Settings"}
        return send_whatsapp_via_wirebase(
            creds.get("wirebase_base_url", ""),
            creds.get("wirebase_api_key", ""),
            creds.get("wirebase_instance_name", ""),
            phone, message,
        )

    evo_instance = creds.get("evo_instance", "")
    if not evo_instance:
        return {"success": False, "error": "WhatsApp instance not configured in Settings"}
    return send_whatsapp_message(evo_instance, phone, message)


# ----------------------------------
# Campaign Flow Execution Engine
# ----------------------------------
def _find_next_node(edges_by_source, node_id, handle=None):
    """Given an adjacency map (node_id -> list of edge dicts), returns the
    id of the node reached by following an outgoing edge from node_id.
    `handle` disambiguates condition-node branches ('true' / 'false')."""
    for e in edges_by_source.get(node_id, []):
        if handle is None or (e.get("source_handle") or None) == handle:
            return e.get("target")
    return None


def execute_campaign_for_lead(campaign, lead, user, owner_id):
    """Walks the campaign's node graph (built in the canvas flow builder)
    for a single lead — starting at the 'start' node and following edges
    until a branch has no further connection."""
    nodes = {n["id"]: n for n in campaign.get("nodes", [])}
    edges_by_source = {}
    for e in campaign.get("edges", []):
        edges_by_source.setdefault(e["source"], []).append(e)

    start_node = next((n for n in nodes.values() if n.get("type") == "start"), None)
    if not start_node:
        return  # flow has no start node — nothing to run

    campaign_id = str(campaign["_id"])
    lead_id = str(lead["_id"])

    creds = user.get("integrations", {})
    gmail_addr   = creds.get("gmail_address", "")
    gmail_pass   = creds.get("gmail_app_password", "")
    resend_key   = creds.get("resend_api_key", "")
    resend_from  = creds.get("resend_from_address", "")
    ai_wa_prompt    = creds.get("ai_whatsapp_prompt", "")
    ai_email_prompt = creds.get("ai_email_prompt", "")

    current_id = _find_next_node(edges_by_source, start_node["id"])
    hops = 0

    while current_id and hops < 200:  # safety cap against accidental loops
        hops += 1
        node = nodes.get(current_id)
        if not node:
            break
        node_type = node.get("type", "")
        data = node.get("data", {}) or {}

        if node_type == "wait":
            amount = float(data.get("amount", 1) or 1)
            unit = data.get("unit", "minutes")
            seconds = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(unit, 60) * amount
            time.sleep(min(seconds, 3600))  # a single wait step can't block forever
            current_id = _find_next_node(edges_by_source, current_id)

        elif node_type == "condition":
            field = data.get("field", "")
            operator = data.get("operator", "exists")
            value = (data.get("value") or "").strip().lower()
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
            current_id = _find_next_node(edges_by_source, current_id, "true" if result else "false")

        elif node_type == "whatsapp":
            phone   = lead.get("phone", "")
            message = data.get("message", "")

            if data.get("use_ai"):
                throttle_ai_call(owner_id)
                ai_result = generate_ai_content(lead, "whatsapp", data.get("ai_instructions", ""), ai_wa_prompt)
                if ai_result.get("success"):
                    message = ai_result.get("message", message)

            message = render_template_vars(message, lead)

            if not phone:
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), current_id, "failed", "whatsapp", "Lead has no phone number")
                _bump_campaign_stat(campaign["_id"], "failed")
            else:
                # Routes through whichever provider the owner has active — Evolution or Wirebase
                send_result = send_whatsapp_dispatch(user, phone, message)
                if send_result.get("success"):
                    channel = creds.get("active_provider", "evo")
                    messages_col.insert_one({
                        "owner_id": owner_id, "lead_id": lead_id, "direction": "out",
                        "channel": channel, "text": message, "created_at": datetime.utcnow(),
                    })
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), current_id, "sent", "whatsapp")
                    _bump_campaign_stat(campaign["_id"], "sent")
                else:
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), current_id, "failed", "whatsapp", send_result.get("error", "Send failed"))
                    _bump_campaign_stat(campaign["_id"], "failed")

            current_id = _find_next_node(edges_by_source, current_id)

        elif node_type == "email":
            provider = data.get("provider", "gmail")
            to_addr  = lead.get("email", "")
            subject  = data.get("subject", "")
            body     = data.get("body", "")

            if data.get("use_ai"):
                throttle_ai_call(owner_id)
                ai_result = generate_ai_content(lead, "email", data.get("ai_instructions", ""), ai_email_prompt)
                if ai_result.get("success"):
                    subject = ai_result.get("subject", subject)
                    body    = ai_result.get("body", body)

            subject = render_template_vars(subject, lead)
            body    = render_template_vars(body, lead)
            channel_name = f"email_{provider}"

            if not to_addr:
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), current_id, "failed", channel_name, "Lead has no email address")
                _bump_campaign_stat(campaign["_id"], "failed")
            else:
                try:
                    # Routes through whichever provider is picked on THIS node — Gmail or Resend
                    if provider == "resend":
                        if not resend_key or not resend_from:
                            raise Exception("Resend not configured in Settings")
                        send_resend_email(resend_key, resend_from, to_addr, subject, body)
                    else:
                        if not gmail_addr or not gmail_pass:
                            raise Exception("Gmail not configured in Settings")
                        send_gmail(gmail_addr, gmail_pass, to_addr, subject, body)
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), current_id, "sent", channel_name)
                    _bump_campaign_stat(campaign["_id"], "sent")
                except Exception as e:
                    _log_execution(owner_id, campaign_id, lead_id, lead.get("name", ""), current_id, "failed", channel_name, str(e))
                    _bump_campaign_stat(campaign["_id"], "failed")

            current_id = _find_next_node(edges_by_source, current_id)

        else:
            current_id = _find_next_node(edges_by_source, current_id)


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

    if not any(n.get("type") == "start" for n in campaign.get("nodes", [])):
        return False  # flow hasn't been built yet

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
        if session.get("account_type") == "admin":
            return redirect("/admin/dashboard")
        return redirect("/dashboard")
    return render_template("/index.html")


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
            "account_type": "user",
            "region": "international",
            "plan_id": None,
            "eva_minutes": 0.0,
            "eva_minutes_used": 0.0,
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
            "wirebase_webhook_token": generate_webhook_token(),
            "team_rr_index": 0,
            "category": "",
            "created_at": datetime.utcnow(),
            "last_login": None,
        })
        flash("Account created successfully")
        return redirect("/login")

    return render_template("signup.html")

ADMIN_SIGNUP_KEY = os.getenv("ADMIN_SIGNUP_KEY", "")  # set this in .env so randoms can't self-promote to admin

@app.route("/admin/signup", methods=["GET", "POST"])
def admin_signup():
    if request.method == "POST":
        username   = request.form.get("username", "").strip().lower()
        email      = request.form.get("email", "").strip().lower()
        phone      = request.form.get("phone", "").strip()
        password   = request.form.get("password", "")
        setup_key  = request.form.get("setup_key", "")

        if not ADMIN_SIGNUP_KEY or setup_key != ADMIN_SIGNUP_KEY:
            flash("Invalid setup key")
            return redirect("/admin/signup")

        if not username:
            flash("Username required"); return redirect("/admin/signup")
        if not email:
            flash("Email required"); return redirect("/admin/signup")
        if not password:
            flash("Password required"); return redirect("/admin/signup")

        if users_col.find_one({"username": username, "type": "user"}):
            flash("Username already exists"); return redirect("/admin/signup")
        if users_col.find_one({"email": email, "type": "user"}):
            flash("Email already exists"); return redirect("/admin/signup")

        users_col.insert_one({
            "type": "user",
            "account_type": "admin",
            "region": "international",
            "plan_id": None,
            "eva_minutes": 0.0,
            "eva_minutes_used": 0.0,
            "username": username,
            "email": email,
            "phone": phone,
            "business_name": "",
            "business_type": "",
            "website": "",
            "address": "",
            "password": generate_password_hash(password),
            "status": "active",
            "email_verified": True,
            "plan": {"name": "Free", "credits": 100},
            "integrations": {},
            "webhook_token": generate_webhook_token(),
            "wirebase_webhook_token": generate_webhook_token(),
            "team_rr_index": 0,
            "created_at": datetime.utcnow(),
            "last_login": None,
        })
        flash("Admin account created successfully. Please log in.")
        return redirect("/login")

    return render_template("admin_signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        # Try account owner / admin first
        user = users_col.find_one({"username": username, "type": "user"})
        if user and check_password_hash(user["password"], password):
            if user.get("status", "active") != "active":
                flash("This account has been disabled. Contact support.")
                return redirect("/login")
            session["user_id"]      = str(user["_id"])
            session["actor_id"]     = str(user["_id"])
            session["username"]     = user["username"]
            session["role"]         = "owner"
            session["account_type"] = user.get("account_type", "user")
            users_col.update_one({"_id": user["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
            if session["account_type"] == "admin":
                return redirect("/admin/dashboard")
            return redirect("/dashboard")

        # Try team member (logs in with email as "username")
        member = teams_col.find_one({"email": username})
        if member and check_password_hash(member["password"], password):
            if member.get("status") != "active":
                flash("Invalid username or password"); return redirect("/login")
            owner = users_col.find_one({"_id": ObjectId(member["owner_id"])})
            if not owner or owner.get("status", "active") != "active":
                flash("This account has been disabled. Contact support.")
                return redirect("/login")
            session["user_id"]      = member["owner_id"]           # data is scoped to the owner
            session["actor_id"]     = str(member["_id"])
            session["username"]     = member.get("name") or member["email"]
            session["role"]         = "member"
            session["account_type"] = "user"
            teams_col.update_one({"_id": member["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
            return redirect("/dashboard")

        flash("Invalid username or password"); return redirect("/login")
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    if session.get("account_type") == "admin":
        return redirect("/admin/dashboard")
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    if not user:
        session.clear(); return redirect("/login")
    return render_template("dashboard.html", user=user, role=session.get("role", "owner"), display_name=session.get("username"))


@app.route("/leads/scrape")
@login_required
@owner_required
def leads_scrape_page():
    return render_template("leads_scrape.html", display_name=session.get("username"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ==================================================================
# WEB WIDGETS  (owner dashboard CRUD)
# ==================================================================

@app.route("/eva/widgets")
@login_required
@owner_required
def eva_widgets_page():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    return render_template("eva_widgets.html", user=user, display_name=session.get("username"))


@app.route("/api/widgets", methods=["GET"])
@login_required
@owner_required
def api_list_widgets():
    widgets = list(widgets_col.find({"owner_id": current_user_id()}).sort("created_at", -1))
    return jsonify({"widgets": [serialize_widget(w) for w in widgets]})


@app.route("/api/widgets", methods=["POST"])
@login_required
@owner_required
def api_create_widget():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    agent_id = data.get("agent_id")
    if not name:
        return jsonify({"error": "Widget name is required"}), 400
    if not agent_id:
        return jsonify({"error": "Select an agent for this widget"}), 400

    agents_col = db["pravah-agents"]
    try:
        agent = agents_col.find_one({"_id": ObjectId(agent_id), "owner_id": current_user_id()})
    except InvalidId:
        return jsonify({"error": "Invalid agent id"}), 400
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    doc = {
        "owner_id": current_user_id(),
        "public_id": generate_public_widget_id(),
        "name": name,
        "agent_id": agent_id,
        "status": "active",
        "primary_color": (data.get("primary_color") or "#2454E8").strip(),
        "greeting": (data.get("greeting") or "Hi! How can I help you today?").strip(),
        "collect_lead": bool(data.get("collect_lead", True)),
        "require_lead_before_chat": bool(data.get("require_lead_before_chat", True)),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    result = widgets_col.insert_one(doc)
    return jsonify({"widget": serialize_widget(widgets_col.find_one({"_id": result.inserted_id}))}), 201


@app.route("/api/widgets/<widget_id>", methods=["PUT", "PATCH"])
@login_required
@owner_required
def api_update_widget(widget_id):
    try: oid = ObjectId(widget_id)
    except InvalidId: return jsonify({"error": "Invalid widget id"}), 400
    data = request.get_json(silent=True) or {}
    update = {"updated_at": datetime.utcnow()}
    if "name" in data: update["name"] = (data["name"] or "").strip()
    if "agent_id" in data:
        agents_col = db["pravah-agents"]
        try:
            agent = agents_col.find_one({"_id": ObjectId(data["agent_id"]), "owner_id": current_user_id()})
        except InvalidId:
            return jsonify({"error": "Invalid agent id"}), 400
        if not agent:
            return jsonify({"error": "Agent not found"}), 404
        update["agent_id"] = data["agent_id"]
    if "primary_color" in data: update["primary_color"] = data["primary_color"]
    if "greeting" in data: update["greeting"] = data["greeting"]
    if "collect_lead" in data: update["collect_lead"] = bool(data["collect_lead"])
    if "require_lead_before_chat" in data: update["require_lead_before_chat"] = bool(data["require_lead_before_chat"])
    if "status" in data and data["status"] in ("active", "paused"):
        update["status"] = data["status"]

    result = widgets_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": update})
    if result.matched_count == 0:
        return jsonify({"error": "Widget not found"}), 404
    return jsonify({"widget": serialize_widget(widgets_col.find_one({"_id": oid}))})


@app.route("/api/widgets/<widget_id>", methods=["DELETE"])
@login_required
@owner_required
def api_delete_widget(widget_id):
    try: oid = ObjectId(widget_id)
    except InvalidId: return jsonify({"error": "Invalid widget id"}), 400
    result = widgets_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0:
        return jsonify({"error": "Widget not found"}), 404
    return jsonify({"deleted": True})


# ==================================================================
# WEB WIDGETS  (server-to-server — called by Eva, X-Eva-Secret auth)
# ==================================================================

@app.route("/api/public/widget-config/<public_id>", methods=["GET"])
def api_public_widget_config(public_id):
    if not EVA_API_SECRET or request.headers.get("X-Eva-Secret") != EVA_API_SECRET:
        return jsonify({"error": "Invalid or missing X-Eva-Secret"}), 401

    widget = widgets_col.find_one({"public_id": public_id})
    if not widget or widget.get("status") != "active":
        return jsonify({"error": "Widget not found or inactive"}), 404

    agents_col = db["pravah-agents"]
    try:
        agent = agents_col.find_one({"_id": ObjectId(widget["agent_id"])})
    except InvalidId:
        agent = None
    if not agent:
        return jsonify({"error": "Widget's agent no longer exists"}), 404

    owner = users_col.find_one({"_id": ObjectId(widget["owner_id"])})
    if not owner or owner.get("status") != "active":
        return jsonify({"error": "Account inactive"}), 403
    remaining = float(owner.get("eva_minutes", 0) or 0) - float(owner.get("eva_minutes_used", 0) or 0)
    if remaining <= 0:
        return jsonify({"error": "Owner is out of Eva minutes"}), 402

    return jsonify({
        "owner_id": str(owner["_id"]),
        "widget_id": str(widget["_id"]),
        "public_id": public_id,
        "require_lead_before_chat": widget.get("require_lead_before_chat", True),
        "agent": {
            "name": agent.get("name", ""),
            "system_prompt": agent.get("system_prompt", ""),
            "gender": agent.get("gender", "female"),
            "language": agent.get("language", "auto"),
            "speaker": agent.get("speaker", ""),
            "opening_line": agent.get("opening_line") or widget.get("greeting") or "Hi, how can I help you today?",
            "min_duration_secs": agent.get("min_duration_secs", 20),
            "max_duration_secs": agent.get("max_duration_secs", 600),
        },
    })


@app.route("/api/eva-webhook/widget-lead", methods=["POST"])
def api_eva_webhook_widget_lead():
    if not EVA_API_SECRET or request.headers.get("X-Eva-Secret") != EVA_API_SECRET:
        return jsonify({"error": "Invalid or missing X-Eva-Secret"}), 401

    data = request.get_json(silent=True) or {}
    owner_id = data.get("owner_id")
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip().lower()
    if not owner_id or not name or not phone:
        return jsonify({"error": "owner_id, name and phone are required"}), 400

    norm = normalize_phone(phone)
    existing = leads_col.find_one({
        "owner_id": owner_id,
        "phone": {"$regex": re.escape(norm[-9:])},
    }) if norm else None

    now = datetime.utcnow()
    if existing:
        leads_col.update_one({"_id": existing["_id"]}, {"$set": {
            "email": email or existing.get("email", ""), "updated_at": now,
        }})
        lead_id = str(existing["_id"])
    else:
        doc = {
            "owner_id": owner_id, "name": name, "business_name": "",
            "email": email, "phone": phone, "website": "", "description": "",
            "source": "web_widget", "status": "warm", "assigned_to": None,
            "ai_task_prompt": "", "segment_id": "", "created_at": now, "updated_at": now,
        }
        inserted = leads_col.insert_one(doc)
        lead_id = str(inserted.inserted_id)
        assigned = assign_round_robin(owner_id)
        if assigned:
            leads_col.update_one({"_id": inserted.inserted_id}, {"$set": {"assigned_to": assigned}})

    return jsonify({"received": True, "lead_id": lead_id})


@app.route("/api/eva-webhook/widget-session-result", methods=["POST"])
def api_eva_webhook_widget_session_result():
    if not EVA_API_SECRET or request.headers.get("X-Eva-Secret") != EVA_API_SECRET:
        return jsonify({"error": "Invalid or missing X-Eva-Secret"}), 401

    data = request.get_json(silent=True) or {}
    owner_id = data.get("owner_id")
    widget_id = data.get("widget_id")
    lead_id = data.get("lead_id")
    duration_secs = float(data.get("duration_secs", 0) or 0)
    transcript = data.get("transcript", [])
    if not owner_id:
        return jsonify({"error": "owner_id is required"}), 400

    minutes_used = round(duration_secs / 60.0, 3)
    if minutes_used > 0:
        try:
            users_col.update_one({"_id": ObjectId(owner_id)}, {"$inc": {"eva_minutes_used": minutes_used}})
        except InvalidId:
            pass

    db["pravah-widget-sessions"].insert_one({
        "owner_id": owner_id, "widget_id": widget_id, "lead_id": lead_id or "",
        "duration_secs": duration_secs, "minutes_used": minutes_used,
        "transcript": transcript, "created_at": datetime.utcnow(),
    })

    if lead_id and transcript:
        try:
            lead = leads_col.find_one({"_id": ObjectId(lead_id)})
            if lead:
                lead_text = " ".join(t.get("text", "") for t in transcript if t.get("role") == "lead")
                if lead_text:
                    new_status = classify_lead_temperature(lead, lead_text, [], lead.get("status", "warm"))
                    leads_col.update_one({"_id": lead["_id"]}, {"$set": {"status": new_status, "updated_at": datetime.utcnow()}})
        except Exception:
            pass

    return jsonify({"received": True, "minutes_used": minutes_used})


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
        "category": user.get("category", ""),
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
    segment_id = request.args.get("segment_id", "").strip()
    status = request.args.get("status", "").strip()
    query = _leads_scope_query()
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"business_name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
        ]
    if segment_id:
        query["segment_id"] = segment_id
    if status in LEAD_STATUSES:
        query["status"] = status
    leads = list(leads_col.find(query).sort("created_at", -1))
    return jsonify({"leads": [serialize_lead(l) for l in leads]})


@app.route("/api/leads", methods=["POST"])
@login_required
def api_create_lead():
    data = request.get_json(silent=True) or {}
    lead = clean_lead_payload(data)
    if not lead["name"]:
        return jsonify({"error": "Name is required"}), 400
    if not lead["phone"]:
        return jsonify({"error": "Phone is required"}), 400
    lead["owner_id"]    = current_user_id()
    lead["source"]      = "manual"
    lead["status"]      = "pending"
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
            lead["status"]      = "pending"
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
    lead's pending/cold/warm/hot status from the sheet."""
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in LEAD_STATUSES:
        return jsonify({"error": "status must be pending, cold, warm, or hot"}), 400
    query = {"_id": oid, "owner_id": current_user_id()}
    if not is_owner():
        query["assigned_to"] = current_actor_id()
    result = leads_col.update_one(query, {"$set": {"status": status, "updated_at": datetime.utcnow()}})
    if result.matched_count == 0:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify({"updated": True, "status": status})


@app.route("/api/leads/<lead_id>/segment", methods=["PATCH"])
@login_required
def api_update_lead_segment(lead_id):
    """Lets the owner OR the assigned team member move a lead into a
    different segment (or clear it) straight from the sheet."""
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    data = request.get_json(silent=True) or {}
    segment_id = (data.get("segment_id") or "").strip()
    if segment_id:
        try:
            if not segments_col.find_one({"_id": ObjectId(segment_id), "owner_id": current_user_id()}):
                return jsonify({"error": "Segment not found"}), 400
        except InvalidId:
            return jsonify({"error": "Invalid segment id"}), 400
    query = {"_id": oid, "owner_id": current_user_id()}
    if not is_owner():
        query["assigned_to"] = current_actor_id()
    result = leads_col.update_one(query, {"$set": {"segment_id": segment_id, "updated_at": datetime.utcnow()}})
    if result.matched_count == 0:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify({"updated": True, "segment_id": segment_id})


@app.route("/api/leads/<lead_id>/ai-toggle", methods=["PATCH"])
@login_required
def api_toggle_lead_ai(lead_id):
    """Lets the owner OR the assigned team member turn the AI auto-reply
    bot on/off for this specific lead's WhatsApp number — e.g. once a human
    has taken over the conversation and shouldn't be interrupted by the bot."""
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    data = request.get_json(silent=True) or {}
    if "ai_disabled" not in data:
        return jsonify({"error": "ai_disabled (true/false) is required"}), 400
    ai_disabled = bool(data.get("ai_disabled"))
    query = {"_id": oid, "owner_id": current_user_id()}
    if not is_owner():
        query["assigned_to"] = current_actor_id()
    result = leads_col.update_one(query, {"$set": {"ai_disabled": ai_disabled, "updated_at": datetime.utcnow()}})
    if result.matched_count == 0:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify({"updated": True, "ai_disabled": ai_disabled})


@app.route("/api/leads/<lead_id>", methods=["DELETE"])
@login_required
def api_delete_lead(lead_id):
    try: oid = ObjectId(lead_id)
    except InvalidId: return jsonify({"error": "Invalid lead id"}), 400
    result = leads_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0: return jsonify({"error": "Lead not found"}), 404
    return jsonify({"deleted": True})


TARGET_LEAD_FIELDS = {"name", "business_name", "email", "phone", "website", "description"}


@app.route("/api/leads/import/preview", methods=["POST"])
@login_required
def api_import_preview():
    """Step 1 of import: parse the file's headers + a few sample rows so the
    frontend can show a column-mapping UI before anything is inserted."""
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
    if df.empty or len(df.columns) == 0:
        return jsonify({"error": "That file doesn't have any columns we can read."}), 400

    columns = [str(c) for c in df.columns]
    suggested_mapping = {}
    for col in columns:
        key = normalize_header(col)
        if key in IMPORT_HEADER_ALIASES:
            suggested_mapping[col] = IMPORT_HEADER_ALIASES[key]

    preview_df = df.head(3).fillna("")
    preview_rows = [[str(v) for v in row] for row in preview_df.astype(str).values.tolist()]

    return jsonify({
        "columns": columns,
        "suggested_mapping": suggested_mapping,
        "preview_rows": preview_rows,
        "row_count": int(len(df)),
    })


@app.route("/api/leads/import", methods=["POST"])
@login_required
def api_import_leads():
    """Step 2 of import: actually insert leads, using the column mapping the
    user confirmed on the preview screen. Falls back to auto-detected
    aliases if no mapping is sent, so older callers still work."""
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

    segment_id = (request.form.get("segment_id") or "").strip()
    if segment_id:
        try:
            if not segments_col.find_one({"_id": ObjectId(segment_id), "owner_id": current_user_id()}):
                segment_id = ""
        except InvalidId:
            segment_id = ""

    mapping_raw = request.form.get("mapping", "")
    column_map = {}
    if mapping_raw:
        try:
            user_mapping = _json.loads(mapping_raw)  # {"<source column>": "name" | "phone" | ...}
        except Exception:
            return jsonify({"error": "Invalid column mapping"}), 400
        for col in df.columns:
            target = (user_mapping.get(str(col)) or "").strip()
            if target in TARGET_LEAD_FIELDS:
                column_map[col] = target
    else:
        for col in df.columns:
            key = normalize_header(col)
            if key in IMPORT_HEADER_ALIASES:
                column_map[col] = IMPORT_HEADER_ALIASES[key]

    if "name" not in column_map.values():
        return jsonify({"error": "Please map a column to 'Name'."}), 400
    if "phone" not in column_map.values():
        return jsonify({"error": "Please map a column to 'Phone'. Name and Phone are required for every lead."}), 400

    df = df.rename(columns=column_map)
    inserted, skipped, now, docs = 0, 0, datetime.utcnow(), []
    for _, row in df.iterrows():
        def clean(field):
            val = row.get(field, "")
            return "" if pd.isna(val) else str(val).strip()
        name  = clean("name")
        phone = clean("phone")
        if not name or name.lower() == "nan" or not phone or phone.lower() == "nan":
            skipped += 1
            continue
        docs.append({
            "owner_id": current_user_id(), "name": name,
            "business_name": clean("business_name"), "email": clean("email").lower(),
            "phone": phone, "website": clean("website"),
            "description": clean("description"), "source": "import",
            "status": "pending", "assigned_to": None, "ai_task_prompt": "",
            "segment_id": segment_id,
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
    leads list view, plus any q/segment_id/status filters) as an Excel file."""
    query = _leads_scope_query()
    q = request.args.get("q", "").strip()
    segment_id = request.args.get("segment_id", "").strip()
    status = request.args.get("status", "").strip()
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"business_name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
        ]
    if segment_id:
        query["segment_id"] = segment_id
    if status in LEAD_STATUSES:
        query["status"] = status
    leads = list(leads_col.find(query).sort("created_at", -1))
    segment_names = {str(s["_id"]): s.get("name", "") for s in segments_col.find({"owner_id": current_user_id()})}
    rows = [{
        "Name": l.get("name", ""), "Business Name": l.get("business_name", ""),
        "Email": l.get("email", ""), "Phone": l.get("phone", ""),
        "Website": l.get("website", ""), "Description": l.get("description", ""),
        "Segment": segment_names.get(l.get("segment_id", ""), ""),
        "Status": l.get("status", "pending"),
    } for l in leads]
    df = pd.DataFrame(rows, columns=["Name", "Business Name", "Email", "Phone", "Website", "Description", "Segment", "Status"])
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

#scraper


@app.route("/api/leads/scrape/start", methods=["POST"])
@login_required
@owner_required
def api_start_lead_scrape():
    """Starts a background job that visits each selected lead's website
    (sitemap first, falling back to a live crawl), pulls their About page,
    and fills in email/description only where those fields are empty."""
    data = request.get_json(silent=True) or {}
    lead_ids = data.get("lead_ids")
    select_all = bool(data.get("all"))
    segment_id = (data.get("segment_id") or "").strip()
    status = (data.get("status") or "").strip()
    owner_id = current_user_id()

    if select_all:
        query = {"owner_id": owner_id, "website": {"$nin": ["", None]}}
        if segment_id:
            query["segment_id"] = segment_id
        if status in LEAD_STATUSES:
            query["status"] = status
        lead_ids = [str(l["_id"]) for l in leads_col.find(query, {"_id": 1})]
    elif not isinstance(lead_ids, list) or not lead_ids:
        return jsonify({"error": "No leads selected"}), 400

    if not lead_ids:
        return jsonify({"error": "No leads with a website in this selection"}), 400

    job_id = _new_scrape_job(owner_id, len(lead_ids))
    threading.Thread(target=run_lead_scrape_job, args=(job_id, owner_id, lead_ids), daemon=True).start()
    return jsonify({"job_id": job_id, "total": len(lead_ids)}), 201


@app.route("/api/leads/scrape/status/<job_id>", methods=["GET"])
@login_required
def api_lead_scrape_status(job_id):
    with _scrape_jobs_lock:
        job = _scrape_jobs.get(job_id)
        if not job or job["owner_id"] != current_user_id():
            return jsonify({"error": "Job not found"}), 404
        return jsonify(dict(job))


@app.route("/api/leads/duplicates", methods=["GET"])
@login_required
@owner_required
def api_find_duplicate_leads():
    """Groups this account's leads by normalized website. Any group of 2+
    is a duplicate set — the oldest lead is kept as the 'original', the
    rest are flagged for deletion."""
    leads = list(leads_col.find(
        {"owner_id": current_user_id(), "website": {"$nin": ["", None]}}
    ).sort("created_at", 1))

    groups = {}
    for l in leads:
        key = normalize_website_for_dedupe(l.get("website", ""))
        if not key:
            continue
        groups.setdefault(key, []).append(l)

    duplicate_groups = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        original, dupes = group[0], group[1:]
        duplicate_groups.append({
            "website": key,
            "original": serialize_lead(original),
            "duplicates": [serialize_lead(d) for d in dupes],
        })

    total_dupes = sum(len(g["duplicates"]) for g in duplicate_groups)
    return jsonify({
        "groups": duplicate_groups,
        "group_count": len(duplicate_groups),
        "duplicate_count": total_dupes,
    })


@app.route("/api/leads/duplicates/delete", methods=["POST"])
@login_required
@owner_required
def api_delete_duplicate_leads():
    data = request.get_json(silent=True) or {}
    lead_ids = data.get("lead_ids", [])
    if not isinstance(lead_ids, list) or not lead_ids:
        return jsonify({"error": "No lead ids provided"}), 400
    oids = []
    for lid in lead_ids:
        try:
            oids.append(ObjectId(lid))
        except InvalidId:
            continue
    result = leads_col.delete_many({"_id": {"$in": oids}, "owner_id": current_user_id()})
    return jsonify({"deleted": result.deleted_count})


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
    if not owner:
        return jsonify({"error": "User not found"}), 404

    result = send_whatsapp_dispatch(owner, lead["phone"], message)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "Send failed")}), 400

    channel = (owner.get("integrations", {}) or {}).get("active_provider", "evo")
    messages_col.insert_one({
        "owner_id": current_user_id(), "lead_id": lead_id, "direction": "out",
        "channel": channel, "text": message, "created_at": datetime.utcnow(),
    })
    return jsonify({"sent": True})


# ==================================================================
# SEGMENTS API  (owner + team — both need the list to tag leads)
# ==================================================================

@app.route("/api/segments", methods=["GET"])
@login_required
def api_list_segments():
    segments = list(segments_col.find({"owner_id": current_user_id()}).sort("created_at", -1))
    return jsonify({"segments": [serialize_segment(s) for s in segments]})


@app.route("/api/segments", methods=["POST"])
@login_required
def api_create_segment():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Segment name is required"}), 400
    existing = segments_col.find_one({
        "owner_id": current_user_id(),
        "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
    })
    if existing:
        return jsonify({"segment": serialize_segment(existing)}), 200
    doc = {"owner_id": current_user_id(), "name": name, "created_at": datetime.utcnow()}
    result = segments_col.insert_one(doc)
    saved = segments_col.find_one({"_id": result.inserted_id})
    return jsonify({"segment": serialize_segment(saved)}), 201


@app.route("/api/segments/<segment_id>", methods=["DELETE"])
@login_required
def api_delete_segment(segment_id):
    try: oid = ObjectId(segment_id)
    except InvalidId: return jsonify({"error": "Invalid segment id"}), 400
    result = segments_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0:
        return jsonify({"error": "Segment not found"}), 404
    # Leads in this segment fall back to "no segment" rather than disappearing
    leads_col.update_many({"owner_id": current_user_id(), "segment_id": segment_id}, {"$set": {"segment_id": ""}})
    return jsonify({"deleted": True})


# ==================================================================
# REAL ESTATE — INVENTORY API  (owner only)
# ==================================================================

@app.route("/api/inventory", methods=["GET"])
@login_required
@owner_required
def api_list_inventory():
    items = list(inventory_col.find({"owner_id": current_user_id(), "status": {"$ne": "deleted"}}).sort("created_at", -1))
    return jsonify({"inventory": [serialize_inventory(i) for i in items]})


@app.route("/api/inventory", methods=["POST"])
@login_required
@owner_required
def api_create_inventory():
    data = request.get_json(silent=True) or {}
    city = (data.get("city") or "").strip()
    headline = (data.get("headline") or "").strip()
    if not headline:
        return jsonify({"error": "Headline is required"}), 400
    if not city:
        return jsonify({"error": "City is required"}), 400
    doc = {
        "owner_id": current_user_id(),
        "view_id": generate_view_id(),
        "headline": headline,
        "description": (data.get("description") or "").strip(),
        "price": (data.get("price") or "").strip(),
        "city": city,
        "area": (data.get("area") or "").strip(),
        "features": data.get("features") or [],
        "images": data.get("images") or [],
        "videos": data.get("videos") or [],
        "pdf_url": (data.get("pdf_url") or "").strip(),
        "status": "active",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    result = inventory_col.insert_one(doc)
    return jsonify({"inventory": serialize_inventory(inventory_col.find_one({"_id": result.inserted_id}))}), 201


@app.route("/api/inventory/<inv_id>", methods=["PUT", "PATCH"])
@login_required
@owner_required
def api_update_inventory(inv_id):
    try: oid = ObjectId(inv_id)
    except InvalidId: return jsonify({"error": "Invalid inventory id"}), 400
    data = request.get_json(silent=True) or {}
    update = {"updated_at": datetime.utcnow()}
    for f in ("headline", "description", "price", "city", "area", "pdf_url"):
        if f in data: update[f] = (data.get(f) or "").strip()
    if "status" in data and data["status"] in ("active", "paused", "deleted"):
        update["status"] = data["status"]
    if "features" in data: update["features"] = data["features"] or []
    if "images" in data: update["images"] = data["images"] or []
    if "videos" in data: update["videos"] = data["videos"] or []
    result = inventory_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": update})
    if result.matched_count == 0:
        return jsonify({"error": "Property not found"}), 404
    return jsonify({"inventory": serialize_inventory(inventory_col.find_one({"_id": oid}))})


@app.route("/api/inventory/<inv_id>", methods=["DELETE"])
@login_required
@owner_required
def api_delete_inventory(inv_id):
    try: oid = ObjectId(inv_id)
    except InvalidId: return jsonify({"error": "Invalid inventory id"}), 400
    result = inventory_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0:
        return jsonify({"error": "Property not found"}), 404
    return jsonify({"deleted": True})


@app.route("/api/inventory/upload/media", methods=["POST"])
@login_required
@owner_required
def api_upload_inventory_media():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    result = upload_media_to_cloudinary(f)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "Upload failed")}), 400
    return jsonify(result)


@app.route("/api/inventory/upload/pdf", methods=["POST"])
@login_required
@owner_required
def api_upload_inventory_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    result = upload_pdf_to_supabase(f, current_user_id())
    if not result.get("success"):
        return jsonify({"error": result.get("error", "Upload failed")}), 400
    return jsonify(result)


# ==================================================================
# PUBLIC PROPERTY VIEW PAGE  (no auth — public link shared with leads)
# ==================================================================

@app.route("/view/<view_id>")
def public_inventory_view(view_id):
    item = inventory_col.find_one({"view_id": view_id, "status": {"$ne": "deleted"}})
    if not item:
        return render_template("inventory_not_found.html"), 404
    owner = users_col.find_one({"_id": ObjectId(item["owner_id"])}) if item.get("owner_id") else None
    return render_template(
        "inventory_view.html",
        item=serialize_inventory(item),
        business_name=(owner.get("business_name") or owner.get("username") or "") if owner else "",
        owner_phone=(owner.get("phone", "") if owner else ""),
    )


# ==================================================================
# SITE VISITS API  (owner only)
# ==================================================================

@app.route("/api/site-visits", methods=["GET"])
@login_required
@owner_required
def api_list_site_visits():
    status = request.args.get("status", "").strip()
    query = {"owner_id": current_user_id()}
    if status in SITE_VISIT_STATUSES:
        query["status"] = status
    visits = list(site_visits_col.find(query).sort("created_at", -1))
    return jsonify({"visits": [serialize_site_visit(v) for v in visits]})


@app.route("/api/site-visits/unread-count", methods=["GET"])
@login_required
@owner_required
def api_site_visits_unread_count():
    count = site_visits_col.count_documents({"owner_id": current_user_id(), "status": "new"})
    return jsonify({"count": count})


@app.route("/api/site-visits/<visit_id>/status", methods=["PATCH"])
@login_required
@owner_required
def api_update_site_visit_status(visit_id):
    try: oid = ObjectId(visit_id)
    except InvalidId: return jsonify({"error": "Invalid visit id"}), 400
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in SITE_VISIT_STATUSES:
        return jsonify({"error": "Invalid status"}), 400
    update = {"status": status}
    if data.get("visit_date"): update["visit_date"] = data["visit_date"]
    if data.get("visit_time"): update["visit_time"] = data["visit_time"]
    result = site_visits_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": update})
    if result.matched_count == 0:
        return jsonify({"error": "Visit not found"}), 404
    return jsonify({"updated": True})

# ==================================================================
# MEETING TEMPLATES API  (owner only)
# ==================================================================

DEFAULT_MEETING_SLOTS = [
    {"day": d, "start": "09:00", "end": "18:00", "available": True}
    for d in ["monday", "tuesday", "wednesday", "thursday", "friday"]
]


@app.route("/api/meeting-template", methods=["GET"])
@login_required
@owner_required
def api_get_meeting_template():
    t = meeting_templates_col.find_one({"owner_id": current_user_id()})
    if not t:
        return jsonify({"template": None})
    return jsonify({"template": serialize_meeting_template(t)})


@app.route("/api/meeting-template", methods=["POST", "PUT"])
@login_required
@owner_required
def api_save_meeting_template():
    data = request.get_json(silent=True) or {}
    duration = int(data.get("duration_minutes", 30) or 30)
    meet_link = (data.get("meet_link") or "").strip()
    admin_whatsapp = (data.get("admin_whatsapp") or "").strip()

    slots_in = data.get("slots")
    if slots_in is None:
        slots_in = DEFAULT_MEETING_SLOTS
    slots = []
    for s in slots_in:
        day = (s.get("day") or "").strip().lower()
        if day not in MEETING_DAYS:
            continue
        slots.append({
            "day": day,
            "start": (s.get("start") or "09:00").strip(),
            "end": (s.get("end") or "18:00").strip(),
            "available": bool(s.get("available", True)),
        })

    update = {
        "owner_id": current_user_id(),
        "duration_minutes": duration,
        "meet_link": meet_link,
        "admin_whatsapp": admin_whatsapp,
        "slots": slots,
        "updated_at": datetime.utcnow(),
    }
    meeting_templates_col.update_one({"owner_id": current_user_id()}, {"$set": update}, upsert=True)
    saved = meeting_templates_col.find_one({"owner_id": current_user_id()})
    return jsonify({"template": serialize_meeting_template(saved)})


@app.route("/api/meeting-template/slots/<int:slot_index>", methods=["PATCH"])
@login_required
@owner_required
def api_toggle_meeting_slot(slot_index):
    """Quick single-slot edit (toggle available / change hours) for the edit UI."""
    data = request.get_json(silent=True) or {}
    t = meeting_templates_col.find_one({"owner_id": current_user_id()})
    if not t or slot_index < 0 or slot_index >= len(t.get("slots", [])):
        return jsonify({"error": "Slot not found"}), 404
    slots = t["slots"]
    if "available" in data:
        slots[slot_index]["available"] = bool(data["available"])
    if "start" in data:
        slots[slot_index]["start"] = data["start"]
    if "end" in data:
        slots[slot_index]["end"] = data["end"]
    meeting_templates_col.update_one({"_id": t["_id"]}, {"$set": {"slots": slots, "updated_at": datetime.utcnow()}})
    return jsonify({"slots": slots})


# ==================================================================
# MEETINGS API  (owner only) — list / stats / status / reschedule
# ==================================================================

@app.route("/api/meetings", methods=["GET"])
@login_required
@owner_required
def api_list_meetings():
    """?range=today|tomorrow|week|all  &status=scheduled|completed|missed|rescheduled|cancelled"""
    range_key = request.args.get("range", "all")
    status = request.args.get("status", "").strip()

    query = {"owner_id": current_user_id()}
    now = datetime.utcnow()
    if range_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        query["scheduled_at"] = {"$gte": start, "$lt": end}
    elif range_key == "tomorrow":
        start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        query["scheduled_at"] = {"$gte": start, "$lt": end}
    elif range_key == "week":
        query["scheduled_at"] = {"$gte": now, "$lte": now + timedelta(days=7)}

    if status in MEETING_STATUSES:
        query["status"] = status

    meetings = list(meetings_col.find(query).sort("scheduled_at", 1))
    return jsonify({"meetings": [serialize_meeting(m) for m in meetings]})


@app.route("/api/meetings/stats", methods=["GET"])
@login_required
def api_meetings_stats():
    """14-day booked-meetings timeseries + status breakdown, for the dashboard chart."""
    owner_id = current_user_id()
    since = datetime.utcnow() - timedelta(days=14)
    daily = {}
    for m in meetings_col.find({"owner_id": owner_id, "created_at": {"$gte": since}}, {"created_at": 1}):
        day = m["created_at"].strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0) + 1
    timeseries = []
    for i in range(13, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        timeseries.append({"date": day, "booked": daily.get(day, 0)})

    status_counts = {s: meetings_col.count_documents({"owner_id": owner_id, "status": s}) for s in MEETING_STATUSES}
    upcoming = meetings_col.count_documents({
        "owner_id": owner_id, "status": "scheduled", "scheduled_at": {"$gte": datetime.utcnow()},
    })
    return jsonify({"timeseries": timeseries, "status_counts": status_counts, "upcoming": upcoming})


@app.route("/api/meetings/<meeting_id>/status", methods=["PATCH"])
@login_required
@owner_required
def api_update_meeting_status(meeting_id):
    try: oid = ObjectId(meeting_id)
    except InvalidId: return jsonify({"error": "Invalid meeting id"}), 400
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in ("completed", "missed", "cancelled", "scheduled"):
        return jsonify({"error": "status must be completed, missed, cancelled, or scheduled"}), 400
    result = meetings_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": {"status": status}})
    if result.matched_count == 0:
        return jsonify({"error": "Meeting not found"}), 404
    return jsonify({"updated": True, "status": status})


@app.route("/api/meetings/<meeting_id>/reschedule", methods=["POST"])
@login_required
@owner_required
def api_reschedule_meeting(meeting_id):
    """Owner manually sets a new date/time — this bypasses the slot-availability
    check since the owner is overriding by hand. Notifies the lead via WhatsApp,
    and (best-effort) has Eva place a fresh call about the new time too."""
    try: oid = ObjectId(meeting_id)
    except InvalidId: return jsonify({"error": "Invalid meeting id"}), 400
    data = request.get_json(silent=True) or {}
    new_dt = parse_iso_utc(data.get("scheduled_at", ""))
    if not new_dt:
        return jsonify({"error": "A valid new date/time is required"}), 400

    old = meetings_col.find_one({"_id": oid, "owner_id": current_user_id()})
    if not old:
        return jsonify({"error": "Meeting not found"}), 404

    meetings_col.update_one({"_id": oid}, {"$set": {"status": "rescheduled"}})

    template = meeting_templates_col.find_one({"owner_id": current_user_id()}) or {}
    new_doc = {
        "owner_id": current_user_id(),
        "lead_id": old.get("lead_id", ""),
        "lead_name": old.get("lead_name", ""),
        "lead_phone": old.get("lead_phone", ""),
        "call_id": "", "agent_id": old.get("agent_id", ""),
        "scheduled_at": new_dt,
        "duration_minutes": old.get("duration_minutes", 30),
        "meet_link": old.get("meet_link") or template.get("meet_link", ""),
        "status": "scheduled",
        "reminder_15_sent": False, "reminder_5_sent": False,
        "rescheduled_from": str(old["_id"]),
        "created_at": datetime.utcnow(),
    }
    inserted = meetings_col.insert_one(new_doc)
    new_meeting = meetings_col.find_one({"_id": inserted.inserted_id})

    owner = users_col.find_one({"_id": ObjectId(current_user_id())})
    when_str = new_dt.strftime("%A, %d %b %Y at %H:%M UTC")
    if owner and old.get("lead_phone"):
        send_meeting_whatsapp(owner, old["lead_phone"],
            f"Your meeting has been rescheduled to {when_str}." +
            (f" Join here: {new_doc['meet_link']}" if new_doc.get("meet_link") else ""))

    call_note = None
    try:
        agents_col_ = db["pravah-agents"]
        voip_col_ = db["pravah-voip"]
        calls_col_ = db["pravah-calls"]
        call_campaigns_col_ = db["pravah-call-campaigns"]
        agent = agents_col_.find_one({"_id": ObjectId(old["agent_id"])}) if old.get("agent_id") else None
        voip = voip_col_.find_one({"owner_id": current_user_id()})
        if agent and voip and voip.get("account_sid") and old.get("lead_phone"):
            reschedule_agent = dict(agent)
            reschedule_agent["opening_line"] = (
                f"Hi {{name}}, quick update — your meeting has been moved to {when_str}. "
                f"Does that still work for you?"
            )
            lead_ctx = {"_id": ObjectId(), "name": old.get("lead_name", ""), "phone": old.get("lead_phone", "")}
            result = place_outbound_call(
                current_user_id(), lead_ctx, reschedule_agent, voip,
                call_campaigns_col_, calls_col_, campaign_id=None,
            )
            call_note = "Eva is calling the lead about the new time." if result.get("success") else result.get("error")
    except Exception as e:
        call_note = f"Could not place reschedule call: {e}"

    return jsonify({"meeting": serialize_meeting(new_meeting), "call_note": call_note})


# ==================================================================
# CAMPAIGNS API
# ==================================================================

@app.route("/api/campaigns", methods=["GET"])
@login_required
@owner_required
def api_list_campaigns():
    campaigns = list(campaigns_col.find({"owner_id": current_user_id()}).sort("created_at", -1))
    return jsonify({"campaigns": [serialize_campaign(c) for c in campaigns]})


DEFAULT_START_NODE = {"id": "start", "type": "start", "x": 40, "y": 160, "data": {}}


@app.route("/api/campaigns", methods=["POST"])
@login_required
@owner_required
def api_create_campaign():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Campaign name is required"}), 400

    nodes = data.get("nodes") or [DEFAULT_START_NODE]
    if not any(n.get("type") == "start" for n in nodes):
        nodes = [DEFAULT_START_NODE] + nodes

    doc = {
        "owner_id":     current_user_id(),
        "name":         name,
        "description":  (data.get("description") or "").strip(),
        "status":       "draft",
        "nodes":        nodes,
        "edges":        data.get("edges", []),
        "lead_ids":     data.get("lead_ids", []),
        "schedule_type": "now",
        "scheduled_at":  None,
        "stats":        {"sent": 0, "failed": 0, "pending": 0},
        "created_at":   datetime.utcnow(),
        "updated_at":   datetime.utcnow(),
        "last_run_at":  None,
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
    if "nodes" in data:
        nodes = data["nodes"] or [DEFAULT_START_NODE]
        if not any(n.get("type") == "start" for n in nodes):
            nodes = [DEFAULT_START_NODE] + nodes
        update["nodes"] = nodes
    if "edges" in data:       update["edges"]       = data["edges"]
    if "lead_ids" in data:    update["lead_ids"]    = data["lead_ids"]
    if "status" in data and data["status"] in ("draft", "active", "paused", "completed", "scheduled"):
        update["status"] = data["status"]

    # Scheduling toggle (used by the Launch modal on the frontend)
    if "schedule_type" in data and data["schedule_type"] in ("now", "schedule"):
        update["schedule_type"] = data["schedule_type"]
        if data["schedule_type"] == "schedule":
            scheduled_at = parse_iso_utc(data.get("scheduled_at", ""))
            if not scheduled_at:
                return jsonify({"error": "A valid scheduled date/time is required"}), 400
            if scheduled_at <= datetime.utcnow():
                return jsonify({"error": "Scheduled time must be in the future"}), 400
            update["scheduled_at"] = scheduled_at
            update["status"] = "scheduled"
        else:
            update["scheduled_at"] = None

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

    # Self-heal tokens for accounts created before these features existed
    webhook_token = user.get("webhook_token")
    if not webhook_token:
        webhook_token = generate_webhook_token()
        users_col.update_one({"_id": user["_id"]}, {"$set": {"webhook_token": webhook_token}})

    wirebase_token = user.get("wirebase_webhook_token")
    if not wirebase_token:
        wirebase_token = generate_webhook_token()
        users_col.update_one({"_id": user["_id"]}, {"$set": {"wirebase_webhook_token": wirebase_token}})

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
        "webhook_url": public_https_url("/webhook/" + webhook_token),
        # Wirebase
        "wirebase_base_url":      creds.get("wirebase_base_url", ""),
        "wirebase_instance_name": creds.get("wirebase_instance_name", ""),
        "wirebase_api_key":       "●●●●●●●●" if creds.get("wirebase_api_key") else "",
        "has_wirebase":           bool(creds.get("wirebase_api_key")),
        "wirebase_webhook_url":   public_https_url("/webhook/wirebase/" + wirebase_token),
        "wirebase_webhook_secret": "●●●●●●●●" if creds.get("wirebase_webhook_secret") else "",
        "has_wirebase_secret":     bool(creds.get("wirebase_webhook_secret")),
        "active_provider":        creds.get("active_provider", "evo"),
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
    maybe_update("wirebase_base_url")
    maybe_update("wirebase_instance_name")
    maybe_update("wirebase_api_key")
    maybe_update("wirebase_webhook_secret")

    if "ai_whatsapp_prompt" in data:
        existing["ai_whatsapp_prompt"] = (data.get("ai_whatsapp_prompt") or "").strip()
    if "ai_email_prompt" in data:
        existing["ai_email_prompt"] = (data.get("ai_email_prompt") or "").strip()
    if data.get("active_provider") in ("evo", "wirebase"):
        existing["active_provider"] = data["active_provider"]

    update = {"integrations": existing}
    if not user.get("webhook_token"):
        update["webhook_token"] = generate_webhook_token()
    if not user.get("wirebase_webhook_token"):
        update["wirebase_webhook_token"] = generate_webhook_token()

    users_col.update_one({"_id": ObjectId(current_user_id())}, {"$set": update})
    return jsonify({"saved": True})


@app.route("/api/whatsapp-bot/diagnose", methods=["GET"])
@login_required
@owner_required
def api_whatsapp_bot_diagnose():
    """Plain-English checklist for why the AI auto-reply might not be firing."""
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    provider = creds.get("active_provider", "evo")

    checks = [
        {"check": "AI bot enabled", "ok": bool(creds.get("ai_bot_enabled"))},
        {"check": "Active provider", "ok": True, "value": provider},
    ]

    if provider == "wirebase":
        checks += [
            {"check": "Wirebase base URL set", "ok": bool(creds.get("wirebase_base_url"))},
            {"check": "Wirebase API key set", "ok": bool(creds.get("wirebase_api_key"))},
            {"check": "Wirebase instance name set", "ok": bool(creds.get("wirebase_instance_name"))},
        ]
    else:
        checks.append({"check": "Evolution instance set", "ok": bool(creds.get("evo_instance"))})
        if creds.get("wirebase_api_key") and not creds.get("evo_instance"):
            checks.append({
                "check": "Mismatch warning",
                "ok": False,
                "value": "Wirebase is configured but active_provider is 'evo' — flip the toggle in Settings",
            })

    checks.append({"check": "MISTRAL_API_KEY set on server", "ok": bool(os.getenv("MISTRAL_API_KEY"))})

    all_ok = all(c["ok"] for c in checks)
    return jsonify({"provider": provider, "all_ok": all_ok, "checks": checks})

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


@app.route("/api/integrations/test/wirebase", methods=["POST"])
@login_required
@owner_required
def api_test_wirebase():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    base_url  = creds.get("wirebase_base_url", "")
    instance  = creds.get("wirebase_instance_name")
    api_key   = creds.get("wirebase_api_key")
    if not (base_url and instance and api_key):
        return jsonify({"success": False, "error": "Fill in Base URL, Instance Name and API Key first"})
    try:
        # NOTE: adjust this path if your Wirebase instance exposes a different status endpoint.
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/public/instance/{instance}/status",
            headers={"X-API-Key": api_key}, timeout=10,
        )
        resp.raise_for_status()
        return jsonify({"success": True, "state": resp.json().get("status", "connected")})
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": str(e)})


# ==================================================================
# WHATSAPP BOT (AI auto-reply) SETTINGS + INBOX  (owner + team)
# ==================================================================

@app.route("/api/whatsapp-bot", methods=["GET"])
@login_required
@owner_required
def api_get_whatsapp_bot():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    return jsonify({
        "ai_bot_enabled": bool(creds.get("ai_bot_enabled", False)),
        "ai_bot_system_prompt": creds.get("ai_bot_system_prompt", ""),
        "active_provider": creds.get("active_provider", "evo"),
    })


@app.route("/api/whatsapp-bot", methods=["POST"])
@login_required
@owner_required
def api_save_whatsapp_bot():
    data = request.get_json(silent=True) or {}
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    existing = user.get("integrations", {}) if user else {}
    existing["ai_bot_enabled"] = bool(data.get("ai_bot_enabled"))
    existing["ai_bot_system_prompt"] = (data.get("ai_bot_system_prompt") or "").strip()
    users_col.update_one({"_id": ObjectId(current_user_id())}, {"$set": {"integrations": existing}})
    return jsonify({"saved": True})


@app.route("/api/whatsapp-bot/webhook-logs", methods=["GET"])
@login_required
@owner_required
def api_whatsapp_webhook_logs():
    """Raw inbound/outbound webhook events for this account — request
    payloads, headers, our decision, and any send response/error — newest
    first. Used by the WhatsApp Bot dashboard's raw debug panel."""
    source = request.args.get("source", "").strip()  # "" | "wirebase" | "evolution"
    query = {"owner_id": current_user_id()}
    if source in ("wirebase", "evolution", "eva"):
        query["source"] = source
    logs = list(webhook_logs_col.find(query).sort("created_at", -1).limit(150))
    return jsonify({"logs": [serialize_webhook_log(d) for d in logs]})


@app.route("/api/whatsapp-bot/inbox", methods=["GET"])
@login_required
def api_whatsapp_inbox():
    """Latest message per lead conversation, most-recent first.
    Team members only see conversations for leads assigned to them."""
    match = {"owner_id": current_user_id()}
    if not is_owner():
        my_lead_ids = [str(l["_id"]) for l in leads_col.find(_leads_scope_query(), {"_id": 1})]
        match["lead_id"] = {"$in": my_lead_ids}

    pipeline = [
        {"$match": match},
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id": "$lead_id",
            "last_text": {"$first": "$text"},
            "last_at": {"$first": "$created_at"},
            "last_direction": {"$first": "$direction"},
            "last_channel": {"$first": "$channel"},
        }},
        {"$sort": {"last_at": -1}},
    ]
    convos = list(messages_col.aggregate(pipeline))
    lead_oids = []
    for c in convos:
        try: lead_oids.append(ObjectId(c["_id"]))
        except (InvalidId, TypeError): pass
    leads_map = {str(l["_id"]): l for l in leads_col.find({"_id": {"$in": lead_oids}})}

    result = []
    for c in convos:
        lead = leads_map.get(c["_id"])
        if not lead: continue
        result.append({
            "lead_id": c["_id"],
            "name": lead.get("name", ""),
            "phone": lead.get("phone", ""),
            "last_text": c["last_text"],
            "last_at": c["last_at"].isoformat() if c.get("last_at") else None,
            "last_direction": c["last_direction"],
            "last_channel": c.get("last_channel", ""),
            "ai_disabled": bool(lead.get("ai_disabled", False)),
        })
    return jsonify({"conversations": result})


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
        "category": user.get("category", ""),
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
    if "category" in data:
        cat = (data.get("category") or "").strip()
        if cat and cat not in BUSINESS_CATEGORIES:
            return jsonify({"error": "Invalid category"}), 400
        update["category"] = cat
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
# ADMIN DASHBOARD PAGE + API  (account_type == "admin" only)
# ==================================================================

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard_page():
    return render_template("admin_dashboard.html", display_name=session.get("username"))


@app.route("/api/admin/stats", methods=["GET"])
@login_required
@admin_required
def api_admin_stats():
    total_users = users_col.count_documents({"type": "user", "account_type": {"$ne": "admin"}})
    active_users = users_col.count_documents({"type": "user", "account_type": {"$ne": "admin"}, "status": "active"})
    disabled_users = users_col.count_documents({"type": "user", "account_type": {"$ne": "admin"}, "status": "disabled"})
    pending_users = users_col.count_documents({"type": "user", "account_type": {"$ne": "admin"}, "email_verified": False})

    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_this_month = users_col.count_documents({
        "type": "user", "account_type": {"$ne": "admin"}, "created_at": {"$gte": month_start},
    })

    plan_counts = []
    for p in plans_col.find({}):
        count = users_col.count_documents({"plan_id": str(p["_id"])})
        plan_counts.append({"plan_id": str(p["_id"]), "name": p.get("name", ""), "region": p.get("region", ""), "users": count})

    no_plan = users_col.count_documents({
        "type": "user", "account_type": {"$ne": "admin"},
        "$or": [{"plan_id": {"$exists": False}}, {"plan_id": None}, {"plan_id": ""}],
    })

    total_eva_minutes_used = 0.0
    for u in users_col.find({"type": "user"}, {"eva_minutes_used": 1}):
        total_eva_minutes_used += float(u.get("eva_minutes_used", 0) or 0)

    return jsonify({
        "total_users": total_users,
        "active_users": active_users,
        "disabled_users": disabled_users,
        "pending_users": pending_users,
        "new_this_month": new_this_month,
        "plan_counts": plan_counts,
        "no_plan_users": no_plan,
        "total_eva_minutes_used": round(total_eva_minutes_used, 2),
        "total_leads": leads_col.count_documents({}),
        "total_team_members": teams_col.count_documents({}),
    })


@app.route("/api/admin/users", methods=["GET"])
@login_required
@admin_required
def api_admin_list_users():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    region = request.args.get("region", "").strip()

    query = {"type": "user", "account_type": {"$ne": "admin"}}
    if q:
        query["$or"] = [
            {"username": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"business_name": {"$regex": q, "$options": "i"}},
        ]
    if status in ("active", "disabled"):
        query["status"] = status
    if region in ("india", "international"):
        query["region"] = region

    users = list(users_col.find(query).sort("created_at", -1))
    return jsonify({"users": [serialize_admin_user(u) for u in users]})


@app.route("/api/admin/users", methods=["POST"])
@login_required
@admin_required
def api_admin_create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or generate_temp_password()
    region   = data.get("region") if data.get("region") in ("india", "international") else "international"

    if not username or not email:
        return jsonify({"error": "Username and email are required"}), 400
    if users_col.find_one({"username": username}):
        return jsonify({"error": "Username already exists"}), 400
    if users_col.find_one({"email": email}):
        return jsonify({"error": "Email already exists"}), 400

    # If an initial plan was chosen, the customer's Eva minutes wallet
    # starts at that plan's included minutes (plus any manual top-up below).
    plan_id = data.get("plan_id") or None
    plan_minutes = 0.0
    if plan_id:
        try:
            plan = plans_col.find_one({"_id": ObjectId(plan_id)})
            if plan:
                plan_minutes = float(plan.get("eva_minutes_included", 0) or 0)
        except InvalidId:
            plan_id = None

    manual_minutes = float(data.get("eva_minutes", 0) or 0)

    doc = {
        "type": "user", "account_type": "user",
        "username": username, "email": email,
        "phone": (data.get("phone") or "").strip(),
        "business_name": (data.get("business_name") or "").strip(),
        "business_type": (data.get("business_type") or "").strip(),
        "website": "", "address": "",
        "password": generate_password_hash(password),
        "status": "active", "email_verified": True,
        "region": region,
        "plan_id": plan_id,
        "eva_minutes": plan_minutes + manual_minutes,
        "eva_minutes_used": 0.0,
        "plan": {"name": "Free", "credits": 100},
        "integrations": {},
        "webhook_token": generate_webhook_token(),
        "wirebase_webhook_token": generate_webhook_token(),
        "team_rr_index": 0,
        "created_at": datetime.utcnow(),
        "last_login": None,
    }
    result = users_col.insert_one(doc)
    saved = users_col.find_one({"_id": result.inserted_id})
    return jsonify({"user": serialize_admin_user(saved), "temp_password": password}), 201


@app.route("/api/admin/users/<user_id>/status", methods=["PATCH"])
@login_required
@admin_required
def api_admin_update_user_status(user_id):
    try: oid = ObjectId(user_id)
    except InvalidId: return jsonify({"error": "Invalid user id"}), 400
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("active", "disabled"):
        return jsonify({"error": "status must be active or disabled"}), 400
    result = users_col.update_one({"_id": oid, "type": "user"}, {"$set": {"status": status}})
    if result.matched_count == 0:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"updated": True, "status": status})


@app.route("/api/admin/users/<user_id>/plan", methods=["PATCH"])
@login_required
@admin_required
def api_admin_assign_plan(user_id):
    """Assigning a plan sets (not increments) the customer's eva_minutes
    wallet to the plan's included-minutes figure — that's their fresh
    allotment for this plan. Use the separate 'add Eva minutes' endpoint
    to top up extra minutes on top of whatever their plan already gives them."""
    try: oid = ObjectId(user_id)
    except InvalidId: return jsonify({"error": "Invalid user id"}), 400
    data = request.get_json(silent=True) or {}
    plan_id = data.get("plan_id")

    update = {"plan_id": plan_id or None}
    if plan_id:
        try:
            plan = plans_col.find_one({"_id": ObjectId(plan_id)})
        except InvalidId:
            return jsonify({"error": "Invalid plan id"}), 400
        if not plan:
            return jsonify({"error": "Plan not found"}), 404
        update["eva_minutes"] = float(plan.get("eva_minutes_included", 0) or 0)
        update["eva_minutes_used"] = 0.0  # fresh plan cycle starts the usage counter over

    result = users_col.update_one({"_id": oid, "type": "user"}, {"$set": update})
    if result.matched_count == 0:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"updated": True, "eva_minutes": update.get("eva_minutes")})


@app.route("/api/admin/users/<user_id>/eva-minutes", methods=["POST"])
@login_required
@admin_required
def api_admin_add_eva_minutes(user_id):
    """Admin grants ADDITIONAL Eva minutes to a customer, on top of
    whatever their plan already gave them. Use a negative number to deduct."""
    try: oid = ObjectId(user_id)
    except InvalidId: return jsonify({"error": "Invalid user id"}), 400
    data = request.get_json(silent=True) or {}
    try:
        minutes = float(data.get("minutes", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "minutes must be a number"}), 400
    if minutes == 0:
        return jsonify({"error": "minutes cannot be 0"}), 400
    result = users_col.update_one({"_id": oid, "type": "user"}, {"$inc": {"eva_minutes": minutes}})
    if result.matched_count == 0:
        return jsonify({"error": "User not found"}), 404
    user = users_col.find_one({"_id": oid})
    return jsonify({"updated": True, "eva_minutes": user.get("eva_minutes", 0)})


@app.route("/api/admin/users/<user_id>/team", methods=["GET"])
@login_required
@admin_required
def api_admin_user_team(user_id):
    members = list(teams_col.find({"owner_id": user_id}).sort("created_at", -1))
    return jsonify({"members": [serialize_team_member(m) for m in members]})


# ---------------- plans ----------------

@app.route("/api/admin/plans", methods=["GET"])
@login_required
@admin_required
def api_admin_list_plans():
    plans = list(plans_col.find({}).sort("created_at", -1))
    return jsonify({"plans": [serialize_plan(p) for p in plans]})


@app.route("/api/admin/plans", methods=["POST"])
@login_required
@admin_required
def api_admin_create_plan():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    region = data.get("region")
    if not name or region not in ("india", "international"):
        return jsonify({"error": "name and a valid region (india/international) are required"}), 400
    doc = {
        "name": name, "region": region,
        "currency": "INR" if region == "india" else "USD",
        "price": float(data.get("price", 0) or 0),
        "billing_cycle": data.get("billing_cycle", "monthly"),
        "eva_minutes_included": float(data.get("eva_minutes_included", 0) or 0),
        "lead_limit": int(data.get("lead_limit", 0) or 0),
        "features": data.get("features", []),
        "created_at": datetime.utcnow(),
    }
    result = plans_col.insert_one(doc)
    return jsonify({"plan": serialize_plan(plans_col.find_one({"_id": result.inserted_id}))}), 201


@app.route("/api/admin/plans/<plan_id>", methods=["PUT", "PATCH"])
@login_required
@admin_required
def api_admin_update_plan(plan_id):
    try: oid = ObjectId(plan_id)
    except InvalidId: return jsonify({"error": "Invalid plan id"}), 400
    data = request.get_json(silent=True) or {}
    update = {}
    for f in ("name", "billing_cycle"):
        if f in data: update[f] = (data[f] or "").strip()
    if "region" in data and data["region"] in ("india", "international"):
        update["region"] = data["region"]
        update["currency"] = "INR" if data["region"] == "india" else "USD"
    if "price" in data: update["price"] = float(data["price"] or 0)
    if "eva_minutes_included" in data: update["eva_minutes_included"] = float(data["eva_minutes_included"] or 0)
    if "lead_limit" in data: update["lead_limit"] = int(data["lead_limit"] or 0)
    if "features" in data: update["features"] = data["features"]
    result = plans_col.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        return jsonify({"error": "Plan not found"}), 404
    return jsonify({"plan": serialize_plan(plans_col.find_one({"_id": oid}))})


@app.route("/api/admin/plans/<plan_id>", methods=["DELETE"])
@login_required
@admin_required
def api_admin_delete_plan(plan_id):
    try: oid = ObjectId(plan_id)
    except InvalidId: return jsonify({"error": "Invalid plan id"}), 400
    result = plans_col.delete_one({"_id": oid})
    if result.deleted_count == 0:
        return jsonify({"error": "Plan not found"}), 404
    users_col.update_many({"plan_id": plan_id}, {"$set": {"plan_id": None}})
    return jsonify({"deleted": True})


# ---------------- Eva per-minute pricing ----------------

@app.route("/api/admin/pricing", methods=["GET"])
@login_required
@admin_required
def api_admin_get_pricing():
    p = pricing_col.find_one({"key": "global"}) or {}
    return jsonify({
        "india_price_per_min": p.get("india_price_per_min", 2.0),
        "international_price_per_min": p.get("international_price_per_min", 0.05),
        "updated_at": p.get("updated_at").isoformat() if p.get("updated_at") else None,
    })


@app.route("/api/admin/pricing", methods=["POST"])
@login_required
@admin_required
def api_admin_save_pricing():
    data = request.get_json(silent=True) or {}
    update = {"updated_at": datetime.utcnow()}
    if "india_price_per_min" in data:
        update["india_price_per_min"] = float(data["india_price_per_min"] or 0)
    if "international_price_per_min" in data:
        update["international_price_per_min"] = float(data["international_price_per_min"] or 0)
    pricing_col.update_one({"key": "global"}, {"$set": update}, upsert=True)
    return jsonify({"saved": True})


# ---------------- Eva agents / campaigns / stats (read-only, cross-account) ----------------

@app.route("/api/admin/eva/agents", methods=["GET"])
@login_required
@admin_required
def api_admin_eva_agents():
    agents_col = db["pravah-agents"]
    owner_map = {str(u["_id"]): u.get("business_name") or u.get("username") for u in users_col.find({}, {"username": 1, "business_name": 1})}
    agents = list(agents_col.find({}).sort("created_at", -1))
    out = [{
        "_id": str(a["_id"]), "name": a.get("name", ""),
        "owner_id": a.get("owner_id", ""), "owner_name": owner_map.get(a.get("owner_id", ""), "Unknown"),
        "gender": a.get("gender", ""), "language": a.get("language", ""),
        "created_at": a.get("created_at").isoformat() if a.get("created_at") else None,
    } for a in agents]
    return jsonify({"agents": out})


@app.route("/api/admin/eva/campaigns", methods=["GET"])
@login_required
@admin_required
def api_admin_eva_campaigns():
    call_campaigns_col = db["pravah-call-campaigns"]
    owner_map = {str(u["_id"]): u.get("business_name") or u.get("username") for u in users_col.find({}, {"username": 1, "business_name": 1})}
    campaigns = list(call_campaigns_col.find({}).sort("created_at", -1).limit(200))
    out = [{
        "_id": str(c["_id"]), "name": c.get("name", ""),
        "owner_id": c.get("owner_id", ""), "owner_name": owner_map.get(c.get("owner_id", ""), "Unknown"),
        "status": c.get("status", ""), "stats": c.get("stats", {}),
        "created_at": c.get("created_at").isoformat() if c.get("created_at") else None,
    } for c in campaigns]
    return jsonify({"campaigns": out})


@app.route("/api/admin/eva/stats", methods=["GET"])
@login_required
@admin_required
def api_admin_eva_stats():
    calls_col = db["pravah-calls"]
    total_calls = calls_col.count_documents({})
    completed = calls_col.count_documents({"status": "completed"})
    failed = calls_col.count_documents({"status": "failed"})
    total_minutes = sum(float(c.get("minutes_used", 0) or 0) for c in calls_col.find({}, {"minutes_used": 1}))

    since = datetime.utcnow() - timedelta(days=14)
    daily = {}
    for c in calls_col.find({"created_at": {"$gte": since}}, {"created_at": 1}):
        day = c["created_at"].strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0) + 1
    timeseries = [{"date": (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d"),
                   "calls": daily.get((datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d"), 0)}
                  for i in range(13, -1, -1)]

    return jsonify({
        "total_calls": total_calls, "completed": completed, "failed": failed,
        "total_minutes": round(total_minutes, 2), "timeseries": timeseries,
    })


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
    data_in = request.get_json(silent=True) or {}
    node_id = data_in.get("node_id")
    phone   = (data_in.get("phone") or "").strip()
    lead_id = data_in.get("lead_id")

    try:
        oid = ObjectId(campaign_id)
    except InvalidId:
        return jsonify({"error": "Invalid campaign id"}), 400

    campaign = campaigns_col.find_one({"_id": oid, "owner_id": current_user_id()})
    if not campaign:
        return jsonify({"error": "Campaign not found"}), 404

    node = next((n for n in campaign.get("nodes", []) if n.get("id") == node_id), None)
    if not node:
        return jsonify({"error": "Node not found — save the flow first"}), 400
    if node.get("type") != "whatsapp":
        return jsonify({"error": "That node is not a WhatsApp node"}), 400

    data = node.get("data", {}) or {}

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

    message = data.get("message", "")
    if data.get("use_ai"):
        ai_prompt  = creds.get("ai_whatsapp_prompt", "")
        ai_result  = generate_ai_content(lead_ctx, "whatsapp", data.get("ai_instructions", ""), ai_prompt)
        if not ai_result.get("success"):
            return jsonify({"error": f"AI generation failed: {ai_result.get('error')}"}), 400
        message = ai_result.get("message", message)

    message = render_template_vars(message, lead_ctx)
    message = f"[TEST] {message}"

    result = send_whatsapp_dispatch(user, target_phone, message)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "Send failed")}), 400

    return jsonify({"sent": True, "to": target_phone, "message": message})





# ==================================================================
# WEBHOOK — receives inbound WhatsApp messages from Evolution API
# Each user gets a unique URL: /webhook/<their_webhook_token>
# ==================================================================


def trim_message_history(owner_id: str, lead_id: str, keep: int = 10):
    """Keeps only the most recent `keep` messages (both directions) for this
    lead's WhatsApp thread. Older messages are deleted so the AI context we
    send stays bounded to the last N and the inbox stays fast."""
    ids_to_keep = [
        m["_id"] for m in messages_col.find(
            {"owner_id": owner_id, "lead_id": lead_id}
        ).sort("created_at", -1).limit(keep)
    ]
    if ids_to_keep:
        messages_col.delete_many({
            "owner_id": owner_id, "lead_id": lead_id,
            "_id": {"$nin": ids_to_keep},
        })

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

        # Keep only the last 10 messages for this lead's thread — bounds what
        # gets sent to the AI and keeps the inbox lightweight.
        trim_message_history(owner_id, lead_id, keep=10)
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

        # 3. Generate and send an AI auto-reply — only if the AI Bot toggle is
        # on globally AND this specific number hasn't been muted from the inbox
        creds = owner.get("integrations", {})
        if not creds.get("ai_bot_enabled"):
            return
        if lead.get("ai_disabled"):
            return

        if owner.get("category") == "real_estate":
            reply = real_estate_chat_reply(owner_id, lead, text, history)
        else:
            reply = generate_chat_reply(
                lead, text, history,
                task_prompt=lead.get("ai_task_prompt", ""),
                system_prompt=creds.get("ai_bot_system_prompt") or creds.get("ai_whatsapp_prompt", ""),
            )
        if not reply.get("success"):
            log("WA-INBOUND", f"owner {owner_id}: AI reply generation failed — {reply.get('error')}")
            return

        if reply.get("message"):
            send_result = send_whatsapp_dispatch(owner, lead.get("phone", phone_raw), reply["message"])
            if send_result.get("success"):
                messages_col.insert_one({
                    "owner_id": owner_id, "lead_id": lead_id, "direction": "out",
                    "channel": creds.get("active_provider", "evo"), "text": reply["message"],
                    "ai_generated": True, "created_at": datetime.utcnow(),
                })
            else:
                log("WA-INBOUND", f"owner {owner_id}: send failed — {send_result.get('error')}")
    except Exception as e:
        log("WA-INBOUND", f"owner {owner_id}: unhandled error — {e}")


@app.route("/webhook/<token>", methods=["POST"])
def webhook_receive(token):
    raw_headers = safe_request_headers()
    payload = request.get_json(silent=True) or {}

    user = users_col.find_one({"webhook_token": token, "type": "user"})
    if not user:
        log_webhook_event(None, "evolution", "inbound", "error",
                           note="Invalid webhook token in URL", payload=payload, headers=raw_headers)
        return jsonify({"error": "Invalid webhook token"}), 404

    owner_id = str(user["_id"])
    data = payload.get("data", payload) or {}
    key = data.get("key", {}) or {}

    if key.get("fromMe"):
        log_webhook_event(owner_id, "evolution", "inbound", "skipped",
                           note="fromMe echo ignored", payload=payload, headers=raw_headers)
        return jsonify({"ok": True}), 200  # ignore our own outgoing messages echoed back

    remote_jid = key.get("remoteJid", "") or data.get("remoteJid", "")
    phone = remote_jid.split("@")[0] if remote_jid else ""
    text = _extract_incoming_text(data.get("message", {}))

    if not phone or not text:
        log_webhook_event(owner_id, "evolution", "inbound", "skipped",
                           note=f"phone={bool(phone)} text={bool(text)}", payload=payload, headers=raw_headers)
        return jsonify({"ok": True}), 200

    log_webhook_event(owner_id, "evolution", "inbound", "received",
                       note=f"From {phone}", payload=payload, headers=raw_headers)

    threading.Thread(target=_process_incoming_whatsapp, args=(owner_id, phone, text), daemon=True).start()
    return jsonify({"received": True}), 200

# ==================================================================
# WEBHOOK — receives inbound WhatsApp messages from Wirebase
# Each user gets a unique URL: /webhook/wirebase/<their_wirebase_webhook_token>
# ==================================================================

def _process_incoming_wirebase(owner_id: str, phone_raw: str, text: str, push_name: str = "", wa_msg_id: str = ""):
    """Runs in a background thread. Saves the inbound message (auto-creating
    the lead if it's a brand new number, so it shows up in the inbox right
    away), scores + assigns it, then sends an AI reply if the bot is on."""
    try:
        owner = users_col.find_one({"_id": ObjectId(owner_id)})
        if not owner:
            return

        norm_phone = normalize_phone(phone_raw)
        lead = leads_col.find_one({
            "owner_id": owner_id,
            "phone": {"$regex": re.escape(norm_phone[-9:])} if norm_phone else "$^",
        }) if norm_phone else None

        if not lead:
            now = datetime.utcnow()
            new_lead = {
                "owner_id": owner_id, "name": push_name or phone_raw, "business_name": "",
                "email": "", "phone": phone_raw, "website": "", "description": "",
                "source": "wirebase", "status": "warm", "assigned_to": None,
                "ai_task_prompt": "", "created_at": now, "updated_at": now,
            }
            inserted = leads_col.insert_one(new_lead)
            lead = leads_col.find_one({"_id": inserted.inserted_id})

        lead_id = str(lead["_id"])

        # 1. Always save the inbound message — this is what makes it show up in the inbox
        messages_col.insert_one({
            "owner_id": owner_id, "lead_id": lead_id, "direction": "in",
            "channel": "wirebase", "text": text, "created_at": datetime.utcnow(),
        })

        # Keep only the last 10 messages for this lead's thread — bounds what
        # gets sent to the AI and keeps the inbox lightweight.
        trim_message_history(owner_id, lead_id, keep=10)
        history = list(messages_col.find({"owner_id": owner_id, "lead_id": lead_id}).sort("created_at", 1))

        # 2. Score + round-robin assign on first-ever reply (same as the Evolution flow)
        new_status = classify_lead_temperature(lead, text, history, lead.get("status", "cold"))
        update = {"status": new_status, "updated_at": datetime.utcnow()}
        if not lead.get("assigned_to"):
            assigned = assign_round_robin(owner_id)
            if assigned:
                update["assigned_to"] = assigned
        leads_col.update_one({"_id": lead["_id"]}, {"$set": update})

        # 3. AI auto-reply, only if the bot toggle is on globally AND this
        # specific number hasn't been muted from the inbox
        creds = owner.get("integrations", {}) or {}
        if not creds.get("ai_bot_enabled"):
            return
        if lead.get("ai_disabled"):
            return

        if owner.get("category") == "real_estate":
            reply = real_estate_chat_reply(owner_id, lead, text, history)
        else:
            reply = generate_chat_reply(
                lead, text, history,
                task_prompt=lead.get("ai_task_prompt", ""),
                system_prompt=creds.get("ai_bot_system_prompt") or creds.get("ai_whatsapp_prompt", ""),
            )
        if not reply.get("success"):
            log("WIREBASE-INBOUND", f"owner {owner_id}: AI reply generation failed — {reply.get('error')}")
            log_webhook_event(owner_id, "wirebase", "outbound", "error",
                               note="AI reply generation failed", response=reply)
            return

        if reply.get("message"):
            send_result = send_whatsapp_dispatch(owner, lead.get("phone", phone_raw), reply["message"])
            if send_result.get("success"):
                messages_col.insert_one({
                    "owner_id": owner_id, "lead_id": lead_id, "direction": "out",
                    "channel": creds.get("active_provider", "evo"), "text": reply["message"],
                    "ai_generated": True, "created_at": datetime.utcnow(),
                })
                log_webhook_event(owner_id, "wirebase", "outbound", "sent",
                                   note=f"AI reply to {lead.get('phone', phone_raw)}",
                                   payload={"message": reply["message"]}, response=send_result)
            else:
                log("WIREBASE-INBOUND", f"owner {owner_id}: send failed — {send_result.get('error')}")
                log_webhook_event(owner_id, "wirebase", "outbound", "error",
                                   note=f"Send failed to {lead.get('phone', phone_raw)}",
                                   payload={"message": reply["message"]}, response=send_result)
    except Exception as e:
        log("WIREBASE-INBOUND", f"owner {owner_id}: unhandled error — {e}")
        log_webhook_event(owner_id, "wirebase", "outbound", "error", note=f"Unhandled error: {e}")



@app.route("/webhook/wirebase/<token>", methods=["POST"])
def wirebase_webhook_receive(token):
    raw_headers = safe_request_headers()
    payload = request.get_json(silent=True) or {}

    user = users_col.find_one({"wirebase_webhook_token": token, "type": "user"})
    if not user:
        log_webhook_event(None, "wirebase", "inbound", "error",
                           note="Invalid webhook token in URL", payload=payload, headers=raw_headers)
        return jsonify({"error": "Invalid webhook token"}), 404

    owner_id = str(user["_id"])

    # Optional extra check: only enforced if the owner saved a signing
    # secret in Settings (see integrations.wirebase_webhook_secret below).
    configured_secret = (user.get("integrations", {}) or {}).get("wirebase_webhook_secret", "")
    if configured_secret and request.headers.get("X-Webhook-Secret", "") != configured_secret:
        log("WIREBASE-WEBHOOK", f"owner {user['_id']}: bad or missing X-Webhook-Secret")
        log_webhook_event(owner_id, "wirebase", "inbound", "error",
                           note="Bad or missing X-Webhook-Secret", payload=payload, headers=raw_headers)
        return jsonify({"error": "Invalid webhook secret"}), 403

    phone_raw = (payload.get("number") or "").strip()
    text      = (payload.get("message") or "").strip()
    is_group  = payload.get("isGroup", False)
    is_lid    = payload.get("isLid", False)
    push_name = payload.get("pushName") or ""
    wa_msg_id = payload.get("messageId")

    if is_group or is_lid or not phone_raw or not text:
        note = f"skipped — group={is_group} lid={is_lid} phone={bool(phone_raw)} text={bool(text)}"
        log("WIREBASE-WEBHOOK", f"owner {user['_id']}: {note}")
        log_webhook_event(owner_id, "wirebase", "inbound", "skipped",
                           note=note, payload=payload, headers=raw_headers)
        return jsonify({"ok": True}), 200

    log_webhook_event(owner_id, "wirebase", "inbound", "received",
                       note=f"From {phone_raw}", payload=payload, headers=raw_headers)

    threading.Thread(
        target=_process_incoming_wirebase,
        args=(owner_id, phone_raw, text, push_name, wa_msg_id),
        daemon=True,
    ).start()
    return jsonify({"received": True}), 200


# ==================================================================
# MEETING BOOKING WEBHOOK  (called by Eva mid-call, X-Eva-Secret auth)
# ==================================================================

@app.route("/api/eva-webhook/book-meeting", methods=["POST"])
def api_eva_webhook_book_meeting():
    """Eva's calling service calls this the moment a lead agrees on a date/time
    during a live (or test) call. We check the owner's availability template +
    existing bookings, confirm or reject, and — on success — send WhatsApp
    confirmations to both the lead and the account's admin number.

    Expected JSON body:
      owner_id, lead_id (optional), lead_name, lead_phone,
      call_id (optional), agent_id (optional),
      requested_datetime  — ISO string, e.g. "2026-08-12T15:00:00" (UTC, naive)
    """
    if not EVA_API_SECRET or request.headers.get("X-Eva-Secret") != EVA_API_SECRET:
        return jsonify({"error": "Invalid or missing X-Eva-Secret"}), 401

    data = request.get_json(silent=True) or {}
    owner_id   = data.get("owner_id")
    lead_id    = data.get("lead_id", "")
    lead_name  = data.get("lead_name", "")
    lead_phone = data.get("lead_phone", "")
    call_id    = data.get("call_id", "")
    agent_id   = data.get("agent_id", "")
    requested  = parse_iso_utc(data.get("requested_datetime", ""))

    if not owner_id or not lead_phone or not requested:
        return jsonify({"error": "owner_id, lead_phone and requested_datetime are required"}), 400

    lead_ctx = {
        "_id": ObjectId(lead_id) if lead_id and ObjectId.is_valid(lead_id) else None,
        "lead_id": lead_id, "name": lead_name, "phone": lead_phone,
    }

    ok, payload = book_meeting(owner_id, lead_ctx, requested, call_id=call_id, agent_id=agent_id)
    return jsonify(payload), (201 if ok else 409)


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
# AI RUN
# ==================================================================

 
EVA_API_BASE_URL = os.environ.get("EVA_API_BASE_URL", "").rstrip("/")
EVA_API_SECRET = os.environ.get("EVA_API_SECRET", "")
PRAVAAH_PUBLIC_BASE_URL = os.environ.get("PRAVAAH_PUBLIC_BASE_URL", "").rstrip("/")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")            # reused, already in .env
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
 
FOLLOWUP_SCAN_INTERVAL_SECS = 60
 
 
def log(stage, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [PRAVAAH-EVA] [{stage}] {msg}", flush=True)
 
 
def _e164(num):
    num = (num or "").strip().replace(" ", "").replace("-", "")
    if num and not num.startswith("+"):
        num = "+" + num
    return num
 
 
def _mistral_chat(system_prompt, user_prompt, force_json=False):
    if not MISTRAL_API_KEY:
        return {"success": False, "error": "MISTRAL_API_KEY not configured"}
    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"},
            json={"model": MISTRAL_MODEL, "temperature": 0.7, "messages": [
                {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt},
            ]},
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if force_json:
            cleaned = text.strip().strip("```json").strip("```").strip()
            return {"success": True, "data": json.loads(cleaned)}
        return {"success": True, "text": text}
    except Exception as e:
        return {"success": False, "error": str(e)}
 
 
def summarize_call(transcript, lead, agent=None):
    convo = "\n".join(f"{t.get('role')}: {t.get('text')}" for t in (transcript or []))
    whatsapp_details = (agent or {}).get("whatsapp_details", "").strip()
    system = (
        "You are analyzing a phone call transcript between a sales voice agent and a lead. "
        "Return ONLY valid JSON, no markdown fences, in this exact shape: "
        '{"summary": "2-3 sentence summary of what was discussed", '
        '"outcome": "interested|not_interested|no_response|callback_requested|unclear", '
        '"lead_status": "hot|warm|cold", '
        '"ai_score": integer 0-100 rating how sales-ready this lead is, '
        '"followup_required": true or false, '
        '"followup_hours": number of hours from now to retry (use 24 if unsure), '
        '"send_whatsapp": true or false — true only if the lead explicitly asked to receive '
        'information/details/pricing over WhatsApp, '
        '"whatsapp_message": "" or a short WhatsApp-ready message with the requested details}'
    )
    user_prompt = (
        f"Lead: {lead.get('name','')} ({lead.get('business_name','')})\n\n"
        f"Transcript:\n{convo or '(no speech captured)'}\n\n"
        f"Information you're allowed to send over WhatsApp if the lead asked for it "
        f"(leave whatsapp_message empty if none applies):\n{whatsapp_details or '(none configured)'}"
    )
    result = _mistral_chat(system, user_prompt, force_json=True)
    if not result.get("success"):
        return {
            "summary": "Summary unavailable.", "outcome": "unclear",
            "lead_status": lead.get("status", "cold"), "ai_score": 0,
            "followup_required": False, "followup_hours": 24,
            "send_whatsapp": False, "whatsapp_message": "",
        }
    d = result["data"]
    lead_status = d.get("lead_status", "cold")
    if lead_status not in ("hot", "warm", "cold"):
        lead_status = "cold"
    try:
        ai_score = max(0, min(100, int(d.get("ai_score", 0))))
    except (TypeError, ValueError):
        ai_score = 0
    return {
        "summary": d.get("summary", ""), "outcome": d.get("outcome", "unclear"),
        "lead_status": lead_status, "ai_score": ai_score,
        "followup_required": bool(d.get("followup_required", False)),
        "followup_hours": float(d.get("followup_hours", 24) or 24),
        "send_whatsapp": bool(d.get("send_whatsapp", False)),
        "whatsapp_message": (d.get("whatsapp_message") or "").strip(),
    } 

def request_call_from_eva(call_id, to_number, twilio_creds, agent, lead, owner_id=None):
    """The ONE place this file talks to Eva's separate service."""
    if not EVA_API_BASE_URL:
        return {"success": False, "error": "EVA_API_BASE_URL not set in PravaahAI's .env"}
    if not PRAVAAH_PUBLIC_BASE_URL:
        return {"success": False, "error": "PRAVAAH_PUBLIC_BASE_URL not set in PravaahAI's .env"}

    meeting_ctx = get_meeting_context(owner_id) if owner_id else None
    agent_system_prompt = agent.get("system_prompt", "")
    if meeting_ctx:
        agent_system_prompt += (
            "\n\nYou can also book meetings on the account owner's calendar. "
            f"Meetings are {meeting_ctx['duration_minutes']} minutes long. "
            f"Available windows: {meeting_ctx['availability_text']}. "
            "The lead's name and phone number are already provided below — never ask for them again. "
            "If the lead wants to schedule a meeting, ask for their preferred date and time, "
            "then confirm it by calling the booking webhook with that date/time. "
            "If the webhook says the slot isn't available, offer one of the alternatives it returns."
        )

    try:
        resp = requests.post(
            f"{EVA_API_BASE_URL}/api/calls",
            headers={"X-Eva-Secret": EVA_API_SECRET, "Content-Type": "application/json"},
            json={
                "call_id": call_id,
                "to_number": to_number,
                "twilio": {
                    "account_sid": twilio_creds.get("account_sid"),
                    "auth_token": twilio_creds.get("auth_token"),
                    "from_number": twilio_creds.get("from_number"),
                },
                "agent": {
                    "name": agent.get("name", ""),
                    "system_prompt": agent_system_prompt,
                    "gender": agent.get("gender", "female"),
                    "language": agent.get("language", "auto"),
                    "speaker": agent.get("speaker", ""),
                    "opening_line": agent.get("opening_line", ""),
                    "min_duration_secs": agent.get("min_duration_secs", 20),
                    "max_duration_secs": agent.get("max_duration_secs", 180),
                },
                "lead": {
                    "name": lead.get("name", ""), "business_name": lead.get("business_name", ""),
                    "email": lead.get("email", ""), "phone": lead.get("phone", ""),
                    "website": lead.get("website", ""), "description": lead.get("description", ""),
                },
                "meeting": ({
                    "owner_id": owner_id,
                    "lead_id": str(lead.get("_id", "")) if lead.get("_id") else "",
                    "agent_id": str(agent.get("_id", "")) if agent.get("_id") else "",
                    **meeting_ctx,
                } if meeting_ctx else None),
                "callback_url": f"{PRAVAAH_PUBLIC_BASE_URL}/api/eva-webhook/call-result",
            },
            timeout=20,
        )
        data = resp.json()
        if resp.status_code >= 400:
            return {"success": False, "error": data.get("error", "Eva rejected the call request")}
        return {"success": True, "call_sid": data.get("call_sid")}
    except Exception as e:
        return {"success": False, "error": str(e)} 

 
def place_outbound_call(owner_id, lead, agent, voip, campaigns_col, calls_col, campaign_id=None):
    to_number = _e164(lead.get("phone", ""))
    from_number = _e164(voip.get("from_number", ""))
    if not to_number or not from_number:
        return {"success": False, "error": "Missing phone number"}
 
    call_doc = {
        "owner_id": owner_id, "campaign_id": campaign_id,
        "lead_id": str(lead["_id"]), "lead_name": lead.get("name", ""),
        "agent_id": str(agent["_id"]), "agent_name": agent.get("name", ""),
        "status": "initiating", "call_sid": None, "created_at": datetime.utcnow(),
    }
    inserted = calls_col.insert_one(call_doc)
    call_id = str(inserted.inserted_id)
 
    result = request_call_from_eva(
        call_id=call_id, to_number=to_number,
        twilio_creds={**voip, "from_number": from_number}, agent=agent, lead=lead,
        owner_id=owner_id,
    )
    if result.get("success"):
        calls_col.update_one({"_id": inserted.inserted_id}, {"$set": {
            "status": "queued", "call_sid": result.get("call_sid"),
        }})
        return {"success": True, "call_id": call_id}
    else:
        calls_col.update_one({"_id": inserted.inserted_id}, {"$set": {
            "status": "failed", "hangup_reason": result.get("error", "Eva request failed"),
            "ended_at": datetime.utcnow(),
        }})
        return {"success": False, "error": result.get("error"), "call_id": call_id}
 
 
# ==================================================================
# Route registration
# ==================================================================
def init_eva(app, db, users_col, leads_col):
    agents_col = db["pravah-agents"]
    voip_col = db["pravah-voip"]
    campaigns_col = db["pravah-call-campaigns"]
    calls_col = db["pravah-calls"]
 
    agents_col.create_index("owner_id")
    voip_col.create_index("owner_id", unique=True)
    campaigns_col.create_index("owner_id")
    calls_col.create_index("owner_id")
    calls_col.create_index("campaign_id")
    calls_col.create_index([("followup_required", 1), ("followup_done", 1), ("followup_at", 1)])
    campaigns_col.create_index([("status", 1), ("scheduled_at", 1)])

    def _parse_iso_utc(s):
        """Parses an ISO datetime string (with or without trailing Z / ms) into
        a naive UTC datetime, matching how the rest of this file stores time."""
        if not s:
            return None
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1]
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
        return None
 
    def current_user_id():
        return session.get("user_id")
 
    def login_required(view):
        from functools import wraps
 
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return jsonify({"error": "Not authenticated"}), 401
            return view(*args, **kwargs)
        return wrapped
 
    def get_remaining_minutes(user):
        total = float(user.get("eva_minutes", 0) or 0)
        used = float(user.get("eva_minutes_used", 0) or 0)
        return max(0.0, total - used)
 
    def serialize_agent(a):
        return {
            "_id": str(a["_id"]), "name": a.get("name", ""), "system_prompt": a.get("system_prompt", ""),
            "gender": a.get("gender", "female"), "language": a.get("language", "auto"),
            "speaker": a.get("speaker", ""), "opening_line": a.get("opening_line", ""),
            "whatsapp_details": a.get("whatsapp_details", ""),
            "min_duration_secs": a.get("min_duration_secs", 20), "max_duration_secs": a.get("max_duration_secs", 180),
            "created_at": a.get("created_at").isoformat() if a.get("created_at") else None,
        }
 
    def serialize_campaign(c):
        return {
            "_id": str(c["_id"]), "name": c.get("name", ""), "agent_id": c.get("agent_id", ""),
            "lead_ids": c.get("lead_ids", []), "status": c.get("status", "draft"),
            "stats": c.get("stats", {"total": 0, "completed": 0, "failed": 0, "pending": 0}),
            "created_at": c.get("created_at").isoformat() if c.get("created_at") else None,
            "last_run_at": c.get("last_run_at").isoformat() if c.get("last_run_at") else None,
            "scheduled_at": c.get("scheduled_at").isoformat() if c.get("scheduled_at") else None,
        }
 
    def serialize_call(c):
        return {
            "_id": str(c["_id"]), "campaign_id": c.get("campaign_id", ""), "lead_id": c.get("lead_id", ""),
            "lead_name": c.get("lead_name", ""), "agent_name": c.get("agent_name", ""),
            "status": c.get("status", "queued"), "call_sid": c.get("call_sid", ""),
            "duration_secs": c.get("duration_secs", 0), "minutes_used": c.get("minutes_used", 0),
            "transcript": c.get("transcript", []), "summary": c.get("summary", ""), "outcome": c.get("outcome", ""),
            "followup_required": c.get("followup_required", False),
            "followup_at": c.get("followup_at").isoformat() if c.get("followup_at") else None,
            "followup_done": c.get("followup_done", False),
            "created_at": c.get("created_at").isoformat() if c.get("created_at") else None,
            "ended_at": c.get("ended_at").isoformat() if c.get("ended_at") else None,
        }
 
    # ---------------- dashboard page ----------------
    @app.route("/eva")
    @login_required
    def eva_dashboard_page():
        user = users_col.find_one({"_id": ObjectId(current_user_id())})
        return render_template("eva_dashboard.html", user=user)
 
    # ---------------- agents ----------------
    @app.route("/api/agents", methods=["GET"])
    @login_required
    def api_list_agents():
        return jsonify({"agents": [serialize_agent(a) for a in agents_col.find({"owner_id": current_user_id()}).sort("created_at", -1)]})
 
    @app.route("/api/agents", methods=["POST"])
    @login_required
    def api_create_agent():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Agent name is required"}), 400
        doc = {
            "owner_id": current_user_id(), "name": name,
            "system_prompt": (data.get("system_prompt") or "").strip(),
            "gender": data.get("gender") if data.get("gender") in ("male", "female") else "female",
            "language": data.get("language") if data.get("language") in ("en", "hi", "auto") else "auto",
            "speaker": (data.get("speaker") or "").strip(),
            "opening_line": (data.get("opening_line") or "Hi {{name}}, do you have a quick minute?").strip(),
            "min_duration_secs": int(data.get("min_duration_secs", 20) or 20),
            "max_duration_secs": int(data.get("max_duration_secs", 180) or 180),
            "whatsapp_details": (data.get("whatsapp_details") or "").strip(),
            "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
        }
        result = agents_col.insert_one(doc)
        return jsonify({"agent": serialize_agent(agents_col.find_one({"_id": result.inserted_id}))}), 201
 
    @app.route("/api/agents/<agent_id>", methods=["PUT", "PATCH"])
    @login_required
    def api_update_agent(agent_id):
        try:
            oid = ObjectId(agent_id)
        except InvalidId:
            return jsonify({"error": "Invalid agent id"}), 400
        data = request.get_json(silent=True) or {}
        update = {"updated_at": datetime.utcnow()}
        for f in ("name", "system_prompt", "speaker", "opening_line", "whatsapp_details"):
            if f in data:
                update[f] = (data.get(f) or "").strip()
        if data.get("gender") in ("male", "female"):
            update["gender"] = data["gender"]
        if data.get("language") in ("en", "hi", "auto"):
            update["language"] = data["language"]
        if "min_duration_secs" in data:
            update["min_duration_secs"] = int(data["min_duration_secs"] or 20)
        if "max_duration_secs" in data:
            update["max_duration_secs"] = int(data["max_duration_secs"] or 180)
        result = agents_col.update_one({"_id": oid, "owner_id": current_user_id()}, {"$set": update})
        if result.matched_count == 0:
            return jsonify({"error": "Agent not found"}), 404
        return jsonify({"agent": serialize_agent(agents_col.find_one({"_id": oid}))})
 
    @app.route("/api/agents/<agent_id>", methods=["DELETE"])
    @login_required
    def api_delete_agent(agent_id):
        try:
            oid = ObjectId(agent_id)
        except InvalidId:
            return jsonify({"error": "Invalid agent id"}), 400
        result = agents_col.delete_one({"_id": oid, "owner_id": current_user_id()})
        if result.deleted_count == 0:
            return jsonify({"error": "Agent not found"}), 404
        return jsonify({"deleted": True})
 
    # ---------------- VOIP creds ----------------
    @app.route("/api/voip", methods=["GET"])
    @login_required
    def api_get_voip():
        doc = voip_col.find_one({"owner_id": current_user_id()}) or {}
        return jsonify({
            "provider": doc.get("provider", "twilio"), "account_sid": doc.get("account_sid", ""),
            "auth_token": "\u25cf" * 8 if doc.get("auth_token") else "",
            "from_number": doc.get("from_number", ""),
            "configured": bool(doc.get("account_sid") and doc.get("auth_token") and doc.get("from_number")),
        })
 
    @app.route("/api/voip", methods=["POST"])
    @login_required
    def api_save_voip():
        data = request.get_json(silent=True) or {}
        existing = voip_col.find_one({"owner_id": current_user_id()}) or {}
        update = {
            "owner_id": current_user_id(),
            "provider": data.get("provider", existing.get("provider", "twilio")),
            "from_number": (data.get("from_number") or existing.get("from_number", "")).strip(),
            "account_sid": (data.get("account_sid") or existing.get("account_sid", "")).strip(),
        }
        if data.get("auth_token") and data["auth_token"] != "\u25cf" * 8:
            update["auth_token"] = data["auth_token"].strip()
        else:
            update["auth_token"] = existing.get("auth_token", "")
        voip_col.update_one({"owner_id": current_user_id()}, {"$set": update}, upsert=True)
        return jsonify({"saved": True})
 
    # ---------------- minutes ----------------
    @app.route("/api/eva-minutes", methods=["GET"])
    @login_required
    def api_eva_minutes():
        user = users_col.find_one({"_id": ObjectId(current_user_id())})
        total = float(user.get("eva_minutes", 0) or 0)
        used = float(user.get("eva_minutes_used", 0) or 0)
        return jsonify({"total": total, "used": round(used, 2), "remaining": round(max(0.0, total - used), 2)})
 
    # ---------------- campaigns ----------------
    @app.route("/api/call-campaigns", methods=["GET"])
    @login_required
    def api_list_call_campaigns():
        return jsonify({"campaigns": [serialize_campaign(c) for c in campaigns_col.find({"owner_id": current_user_id()}).sort("created_at", -1)]})
 
    @app.route("/api/call-campaigns", methods=["POST"])
    @login_required
    def api_create_call_campaign():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        agent_id = data.get("agent_id")
        if not name or not agent_id:
            return jsonify({"error": "name and agent_id are required"}), 400

        schedule_type = data.get("schedule_type", "now")  # "now" | "schedule"
        scheduled_at = None
        if schedule_type == "schedule":
            scheduled_at = _parse_iso_utc(data.get("scheduled_at", ""))
            if not scheduled_at:
                return jsonify({"error": "A valid scheduled date/time is required"}), 400
            if scheduled_at <= datetime.utcnow():
                return jsonify({"error": "Scheduled time must be in the future"}), 400

        doc = {
            "owner_id": current_user_id(), "name": name, "agent_id": agent_id,
            "lead_ids": data.get("lead_ids", []),
            "status": "scheduled" if schedule_type == "schedule" else "draft",
            "scheduled_at": scheduled_at,
            "stats": {"total": 0, "completed": 0, "failed": 0, "pending": 0},
            "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(), "last_run_at": None,
        }
        result = campaigns_col.insert_one(doc)
        campaign_id = str(result.inserted_id)

        if schedule_type == "now":
            ok, payload = launch_call_campaign_now(campaign_id, current_user_id(), doc["lead_ids"] or "all")
            if not ok:
                # Campaign still exists as a draft so the user can retry/fix and launch manually
                return jsonify({
                    "campaign": serialize_campaign(campaigns_col.find_one({"_id": result.inserted_id})),
                    "launch_error": payload.get("error"),
                }), 201

        return jsonify({"campaign": serialize_campaign(campaigns_col.find_one({"_id": result.inserted_id}))}), 201

    @app.route("/api/call-campaigns/<campaign_id>", methods=["DELETE"])
    @login_required
    def api_delete_call_campaign(campaign_id):
        try:
            oid = ObjectId(campaign_id)
        except InvalidId:
            return jsonify({"error": "Invalid campaign id"}), 400
        result = campaigns_col.delete_one({"_id": oid, "owner_id": current_user_id()})
        if result.deleted_count == 0:
            return jsonify({"error": "Campaign not found"}), 404
        return jsonify({"deleted": True})
    @app.route("/api/call-campaigns/<campaign_id>", methods=["GET"])
    @login_required
    def api_get_call_campaign(campaign_id):
        try:
            oid = ObjectId(campaign_id)
        except InvalidId:
            return jsonify({"error": "Invalid campaign id"}), 400
        c = campaigns_col.find_one({"_id": oid, "owner_id": current_user_id()})
        if not c:
            return jsonify({"error": "Campaign not found"}), 404
        return jsonify({"campaign": serialize_campaign(c)})
 
    @app.route("/api/call-campaigns/<campaign_id>/logs", methods=["GET"])
    @login_required
    def api_call_campaign_logs(campaign_id):
        calls = list(calls_col.find({"campaign_id": campaign_id, "owner_id": current_user_id()}).sort("created_at", -1))
        return jsonify({"calls": [serialize_call(c) for c in calls]})
 
    def launch_call_campaign_now(campaign_id, owner_id, lead_ids_target=None):
        """Core launch logic, callable from the launch endpoint, from
        'run now' on creation, and from the scheduler thread alike.
        Returns (ok: bool, payload: dict)."""
        try:
            oid = ObjectId(campaign_id)
        except InvalidId:
            return False, {"error": "Invalid campaign id"}

        campaign = campaigns_col.find_one({"_id": oid, "owner_id": owner_id})
        if not campaign:
            return False, {"error": "Campaign not found"}

        agent = agents_col.find_one({"_id": ObjectId(campaign["agent_id"]), "owner_id": owner_id})
        if not agent:
            return False, {"error": "Campaign's agent no longer exists"}

        voip = voip_col.find_one({"owner_id": owner_id})
        if not voip or not (voip.get("account_sid") and voip.get("auth_token") and voip.get("from_number")):
            return False, {"error": "Add your Twilio/VOIP credentials in Settings before launching calls"}

        target = lead_ids_target if lead_ids_target is not None else (campaign.get("lead_ids") or "all")
        if target == "all" or not target:
            lead_ids = [str(l["_id"]) for l in leads_col.find({"owner_id": owner_id}, {"_id": 1})]
        else:
            lead_ids = target

        leads = list(leads_col.find({
            "_id": {"$in": [ObjectId(lid) for lid in lead_ids if ObjectId.is_valid(lid)]},
            "owner_id": owner_id,
        }))
        leads = [l for l in leads if l.get("phone")]
        if not leads:
            return False, {"error": "No leads with phone numbers to call"}

        user = users_col.find_one({"_id": ObjectId(owner_id)})
        if get_remaining_minutes(user) <= 0:
            return False, {"error": "You are out of Eva minutes. Top up to launch calls."}

        campaigns_col.update_one({"_id": campaign["_id"]}, {"$set": {
            "status": "running", "last_run_at": datetime.utcnow(),
            "stats": {"total": len(leads), "completed": 0, "failed": 0, "pending": len(leads)},
        }})

        def run():
            for lead in leads:
                fresh_user = users_col.find_one({"_id": ObjectId(owner_id)})
                if get_remaining_minutes(fresh_user) <= 0:
                    log("CAMPAIGN", f"{campaign_id}: out of minutes, stopping")
                    break
                place_outbound_call(owner_id, lead, agent, voip, campaigns_col, calls_col, campaign_id=str(campaign["_id"]))
                time.sleep(3)
            remaining_running = calls_col.count_documents({
                "campaign_id": str(campaign["_id"]),
                "status": {"$in": ["queued", "initiating", "in-progress", "ringing"]},
            })
            if remaining_running == 0:
                campaigns_col.update_one({"_id": campaign["_id"]}, {"$set": {"status": "completed"}})

        threading.Thread(target=run, daemon=True, name=f"Campaign-{campaign_id}").start()
        return True, {"launched": True, "leads_queued": len(leads)}

    @app.route("/api/call-campaigns/<campaign_id>/launch", methods=["POST"])
    @login_required
    def api_call_launch_campaign(campaign_id):
        data = request.get_json(silent=True) or {}
        ok, payload = launch_call_campaign_now(campaign_id, current_user_id(), data.get("lead_ids", "all"))
        return jsonify(payload), (200 if ok else 400)
    
    # ---------------- calls ----------------
    @app.route("/api/calls", methods=["GET"])
    @login_required
    def api_list_calls():
        campaign_id = request.args.get("campaign_id")
        query = {"owner_id": current_user_id()}
        if campaign_id:
            query["campaign_id"] = campaign_id
        return jsonify({"calls": [serialize_call(c) for c in calls_col.find(query).sort("created_at", -1).limit(300)]})
 
    @app.route("/api/calls/<call_id>", methods=["GET"])
    @login_required
    def api_get_call(call_id):
        try:
            oid = ObjectId(call_id)
        except InvalidId:
            return jsonify({"error": "Invalid call id"}), 400
        c = calls_col.find_one({"_id": oid, "owner_id": current_user_id()})
        if not c:
            return jsonify({"error": "Call not found"}), 404
        return jsonify({"call": serialize_call(c)})
 
    @app.route("/api/leads/<lead_id>/call-now", methods=["POST"])
    @login_required
    def api_call_now(lead_id):
        data = request.get_json(silent=True) or {}
        agent_id = data.get("agent_id")
        try:
            lead = leads_col.find_one({"_id": ObjectId(lead_id), "owner_id": current_user_id()})
            agent = agents_col.find_one({"_id": ObjectId(agent_id), "owner_id": current_user_id()})
        except InvalidId:
            return jsonify({"error": "Invalid id"}), 400
        if not lead or not lead.get("phone"):
            return jsonify({"error": "Lead not found or has no phone number"}), 400
        if not agent:
            return jsonify({"error": "Agent not found"}), 400
        voip = voip_col.find_one({"owner_id": current_user_id()})
        if not voip or not (voip.get("account_sid") and voip.get("auth_token") and voip.get("from_number")):
            return jsonify({"error": "Add your Twilio/VOIP credentials in Settings first"}), 400
        user = users_col.find_one({"_id": ObjectId(current_user_id())})
        if get_remaining_minutes(user) <= 0:
            return jsonify({"error": "You are out of Eva minutes"}), 400
 
        result = place_outbound_call(current_user_id(), lead, agent, voip, campaigns_col, calls_col, campaign_id=None)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Could not place call")}), 400
        return jsonify({"call_id": result["call_id"]})
 
    @app.route("/api/test-call", methods=["POST"])
    @login_required
    def api_test_call():
        """Ad-hoc test call: no lead needs to exist in the DB — just an
        agent, a name, and a phone number, typed straight from the dashboard."""
        data = request.get_json(silent=True) or {}
        agent_id = data.get("agent_id")
        phone = (data.get("phone") or "").strip()
        name = (data.get("name") or "").strip() or "Test Lead"

        if not agent_id or not phone:
            return jsonify({"error": "agent_id and phone are required"}), 400

        try:
            agent = agents_col.find_one({"_id": ObjectId(agent_id), "owner_id": current_user_id()})
        except InvalidId:
            return jsonify({"error": "Invalid agent id"}), 400
        if not agent:
            return jsonify({"error": "Agent not found"}), 400

        voip = voip_col.find_one({"owner_id": current_user_id()})
        if not voip or not (voip.get("account_sid") and voip.get("auth_token") and voip.get("from_number")):
            return jsonify({"error": "Add your Twilio/VOIP credentials in Settings first"}), 400

        user = users_col.find_one({"_id": ObjectId(current_user_id())})
        if get_remaining_minutes(user) <= 0:
            return jsonify({"error": "You are out of Eva minutes"}), 400

        test_lead = {
            "_id": ObjectId(), "name": name, "business_name": "", "email": "",
            "phone": phone, "website": "", "description": "",
        }
        result = place_outbound_call(current_user_id(), test_lead, agent, voip, campaigns_col, calls_col, campaign_id=None)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Could not place call")}), 400
        return jsonify({"call_id": result["call_id"]})

    
    # ---------------- webhook FROM Eva ----------------
    @app.route("/api/eva-webhook/call-result", methods=["POST"])
    def api_eva_webhook_call_result():
        if not EVA_API_SECRET or request.headers.get("X-Eva-Secret") != EVA_API_SECRET:
            return jsonify({"error": "Invalid or missing X-Eva-Secret"}), 401
 
        data = request.get_json(silent=True) or {}
        call_id = data.get("call_id")
        try:
            oid = ObjectId(call_id)
        except (InvalidId, TypeError):
            return jsonify({"error": "Invalid call_id"}), 400
 
        call_doc = calls_col.find_one({"_id": oid})
        if not call_doc:
            return jsonify({"error": "Unknown call_id"}), 404
 
        status = data.get("status", "completed")
        transcript = data.get("transcript", [])
        duration_secs = float(data.get("duration_secs", 0) or 0)
        minutes_used = round(duration_secs / 60.0, 3)
 
        lead = leads_col.find_one({"_id": ObjectId(call_doc["lead_id"])}) or {"name": call_doc.get("lead_name", "")}
        agent = agents_col.find_one({"_id": ObjectId(call_doc["agent_id"])}) if call_doc.get("agent_id") else None
        summary = summarize_call(transcript, lead, agent) if status == "completed" else {
            "summary": "", "outcome": "no_response", "lead_status": lead.get("status", "cold"),
            "ai_score": 0, "followup_required": False, "followup_hours": 24,
            "send_whatsapp": False, "whatsapp_message": "",
        }
 
        update = {
            "status": status, "hangup_reason": data.get("hangup_reason", status),
            "duration_secs": duration_secs, "minutes_used": minutes_used, "transcript": transcript,
            "summary": summary["summary"], "outcome": summary["outcome"],
            "followup_required": summary["followup_required"], "followup_done": False,
            "ended_at": datetime.utcnow(),
        }
        if summary["followup_required"]:
            update["followup_at"] = datetime.utcnow() + timedelta(hours=summary["followup_hours"])
 
        calls_col.update_one({"_id": oid}, {"$set": update})
 
        if lead.get("_id"):
            leads_col.update_one({"_id": lead["_id"]}, {"$set": {
                "status": summary["lead_status"], "ai_score": summary["ai_score"],
                "updated_at": datetime.utcnow(),
            }})

        if summary["send_whatsapp"] and summary["whatsapp_message"] and lead.get("phone"):
            owner = users_col.find_one({"_id": ObjectId(call_doc["owner_id"])})
            if owner:
                send_result = send_whatsapp_dispatch(owner, lead["phone"], summary["whatsapp_message"])
                if send_result.get("success"):
                    messages_col.insert_one({
                        "owner_id": call_doc["owner_id"], "lead_id": str(lead["_id"]), "direction": "out",
                        "channel": (owner.get("integrations", {}) or {}).get("active_provider", "evo"),
                        "text": summary["whatsapp_message"], "ai_generated": True,
                        "created_at": datetime.utcnow(),
                    })
 
        if minutes_used > 0:
            users_col.update_one({"_id": ObjectId(call_doc["owner_id"])}, {"$inc": {"eva_minutes_used": minutes_used}})
 
        if call_doc.get("campaign_id"):
            bump_field = "stats.completed" if status == "completed" else "stats.failed"
            try:
                campaigns_col.update_one(
                    {"_id": ObjectId(call_doc["campaign_id"])},
                    {"$inc": {bump_field: 1, "stats.pending": -1}},
                )
            except Exception:
                pass
 
        return jsonify({"received": True})
 
    # ---------------- background follow-up scheduler ----------------
    def _followup_scanner():
        while True:
            time.sleep(FOLLOWUP_SCAN_INTERVAL_SECS)
            try:
                due = list(calls_col.find({
                    "followup_required": True, "followup_done": {"$ne": True},
                    "followup_at": {"$lte": datetime.utcnow()},
                }))
            except Exception as e:
                log("FOLLOWUP", f"scan error: {e}")
                continue
            for call_doc in due:
                calls_col.update_one({"_id": call_doc["_id"]}, {"$set": {"followup_done": True}})
                owner_id = call_doc.get("owner_id")
                user = users_col.find_one({"_id": ObjectId(owner_id)}) if owner_id else None
                if not user or get_remaining_minutes(user) <= 0:
                    continue
                try:
                    lead = leads_col.find_one({"_id": ObjectId(call_doc["lead_id"])})
                    agent = agents_col.find_one({"_id": ObjectId(call_doc["agent_id"])})
                    voip = voip_col.find_one({"owner_id": owner_id})
                except Exception:
                    continue
                if not (lead and agent and voip):
                    continue
                place_outbound_call(owner_id, lead, agent, voip, campaigns_col, calls_col, campaign_id=call_doc.get("campaign_id"))
 
    threading.Thread(target=_followup_scanner, daemon=True, name="PravaahEvaFollowupScanner").start()

    # ---------------- background campaign scheduler ----------------
    def _campaign_scheduler():
        while True:
            time.sleep(30)  # check twice a minute so scheduled campaigns fire promptly
            try:
                due = list(campaigns_col.find({
                    "status": "scheduled",
                    "scheduled_at": {"$lte": datetime.utcnow()},
                }))
            except Exception as e:
                log("SCHEDULER", f"scan error: {e}")
                continue
            for c in due:
                campaign_id = str(c["_id"])
                owner_id = c.get("owner_id")
                ok, payload = launch_call_campaign_now(campaign_id, owner_id, c.get("lead_ids") or "all")
                if not ok:
                    log("SCHEDULER", f"{campaign_id} failed to launch: {payload.get('error')}")
                    campaigns_col.update_one({"_id": c["_id"]}, {"$set": {"status": "draft"}})

    threading.Thread(target=_campaign_scheduler, daemon=True, name="PravaahEvaCampaignScheduler").start()

    log("INIT", "PravaahAI Eva-dashboard routes registered.")

def _wa_campaign_scheduler():
    """Fires scheduled WhatsApp/Email campaigns the moment their time comes."""
    while True:
        time.sleep(30)
        try:
            due = list(campaigns_col.find({
                "status": "scheduled",
                "scheduled_at": {"$lte": datetime.utcnow()},
            }))
        except Exception:
            continue
        for c in due:
            ok = launch_campaign(str(c["_id"]), c["owner_id"])
            if not ok:
                # couldn't launch (e.g. leads removed) — drop back to draft so it's not retried forever
                campaigns_col.update_one({"_id": c["_id"]}, {"$set": {"status": "draft"}})


def _meeting_reminder_scanner():
    """Every minute, sends a 15-min-before and 5-min-before WhatsApp reminder
    to both the lead and the account's configured admin number."""
    while True:
        time.sleep(60)
        try:
            now = datetime.utcnow()
            upcoming = list(meetings_col.find({
                "status": "scheduled",
                "scheduled_at": {"$gte": now, "$lte": now + timedelta(minutes=16)},
            }))
        except Exception:
            continue
        for m in upcoming:
            minutes_out = (m["scheduled_at"] - now).total_seconds() / 60.0
            owner = users_col.find_one({"_id": ObjectId(m["owner_id"])})
            if not owner:
                continue
            template = meeting_templates_col.find_one({"owner_id": m["owner_id"]}) or {}
            when_str = m["scheduled_at"].strftime("%H:%M UTC")

            if not m.get("reminder_15_sent") and 14.5 <= minutes_out <= 15.5:
                msg_lead = f"Reminder: your meeting is in 15 minutes ({when_str})." + (f" {m.get('meet_link')}" if m.get("meet_link") else "")
                msg_admin = f"Reminder: meeting with {m.get('lead_name','a lead')} in 15 minutes ({when_str})."
                if m.get("lead_phone"): send_meeting_whatsapp(owner, m["lead_phone"], msg_lead)
                if template.get("admin_whatsapp"): send_meeting_whatsapp(owner, template["admin_whatsapp"], msg_admin)
                meetings_col.update_one({"_id": m["_id"]}, {"$set": {"reminder_15_sent": True}})

            elif not m.get("reminder_5_sent") and 4.5 <= minutes_out <= 5.5:
                msg_lead = f"Reminder: your meeting starts in 5 minutes ({when_str})." + (f" {m.get('meet_link')}" if m.get("meet_link") else "")
                msg_admin = f"Reminder: meeting with {m.get('lead_name','a lead')} starts in 5 minutes ({when_str})."
                if m.get("lead_phone"): send_meeting_whatsapp(owner, m["lead_phone"], msg_lead)
                if template.get("admin_whatsapp"): send_meeting_whatsapp(owner, template["admin_whatsapp"], msg_admin)
                meetings_col.update_one({"_id": m["_id"]}, {"$set": {"reminder_5_sent": True}})


threading.Thread(target=_wa_campaign_scheduler, daemon=True, name="PravaahCampaignScheduler").start()
threading.Thread(target=_meeting_reminder_scanner, daemon=True, name="PravaahMeetingReminderScanner").start()

init_eva(app, db, users_col, leads_col)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 6875)), debug=True)