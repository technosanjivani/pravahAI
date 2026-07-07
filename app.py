from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    flash,
    url_for,
    jsonify,
    Response
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

from datetime import datetime
import os
import io
import csv
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

# ----------------------------------
# Create Indexes
# ----------------------------------

try:
    users_col.create_index("username", unique=True)
    users_col.create_index("email", unique=True)
    leads_col.create_index("owner_id")
    leads_col.create_index([("owner_id", 1), ("created_at", -1)])
    campaigns_col.create_index("owner_id")
    executions_col.create_index([("campaign_id", 1), ("lead_id", 1)])
    executions_col.create_index("owner_id")
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


def normalize_header(h):
    return str(h).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


# ----------------------------------
# Auth Helper
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


def current_user_id():
    return session.get("user_id")


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


def generate_ai_content(lead: dict, content_type: str, instructions: str = "") -> dict:
    """
    Calls Mistral with the lead's full data (name -> description) and returns
    a generated WhatsApp message, or an email subject+body.
    """
    if not MISTRAL_API_KEY:
        return {"success": False, "error": "MISTRAL_API_KEY not configured"}

    lead_context = (
        f"Name: {lead.get('name','')}\n"
        f"Business Name: {lead.get('business_name','')}\n"
        f"Email: {lead.get('email','')}\n"
        f"Phone: {lead.get('phone','')}\n"
        f"Website: {lead.get('website','')}\n"
        f"Description: {lead.get('description','')}\n"
    )

    if content_type == "whatsapp":
        system_prompt = (
            "You are a sales outreach assistant. Write a short, friendly, personalized "
            "WhatsApp message (2-4 sentences) to this lead using their real data below. "
            "No placeholders. Return ONLY the message text, nothing else."
        )
    else:
        system_prompt = (
            "You are a sales outreach assistant. Write a personalized outreach email "
            "for this lead using their real data below. Return ONLY valid JSON in the "
            'exact shape {"subject": "...", "body": "..."} with no markdown fences and '
            "no extra text. Body may use simple HTML paragraph tags."
        )

    user_prompt = f"Lead data:\n{lead_context}"
    if instructions:
        user_prompt += f"\nAdditional instructions: {instructions}\n"

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

        if content_type == "whatsapp":
            return {"success": True, "message": text}

        import json as _json
        cleaned = text.strip().strip("```json").strip("```").strip()
        parsed = _json.loads(cleaned)
        return {"success": True, "subject": parsed.get("subject", ""), "body": parsed.get("body", "")}

    except Exception as e:
        return {"success": False, "error": str(e)}
    
    

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

    step_idx = 0
    while step_idx < len(flow):
        step = flow[step_idx]
        step_type = step.get("type", "")

        if step_type == "wait":
            unit    = step.get("unit", "minutes")
            amount  = int(step.get("amount", 1))
            seconds = amount * {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(unit, 60)
            time.sleep(seconds)
            step_idx += 1
            continue

        if step_type == "condition":
            field    = step.get("field", "")
            operator = step.get("operator", "exists")
            value    = step.get("value", "")
            lead_val = lead.get(field, "")

            matched = False
            if operator == "exists":         matched = bool(lead_val)
            elif operator == "not_exists":   matched = not bool(lead_val)
            elif operator == "equals":       matched = str(lead_val).lower() == str(value).lower()
            elif operator == "contains":     matched = str(value).lower() in str(lead_val).lower()
            elif operator == "not_contains": matched = str(value).lower() not in str(lead_val).lower()

            then_idx = step.get("then_step")
            else_idx = step.get("else_step")
            if matched and then_idx is not None:
                step_idx = then_idx
            elif not matched and else_idx is not None:
                step_idx = else_idx
            else:
                step_idx += 1
            continue

        # ── WHATSAPP ──────────────────────────
        if step_type == "whatsapp":
            phone   = lead.get("phone", "")
            message = step.get("message", "")

            if step.get("use_ai"):
                ai_result = generate_ai_content(lead, "whatsapp", step.get("ai_instructions", ""))
                if ai_result.get("success"):
                    message = ai_result.get("message", message)

            message = render_template_vars(message, lead)

            if not phone:
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name",""), step_idx, "failed", "whatsapp", "Lead has no phone number")
                _bump_campaign_stat(campaign["_id"], "failed")
            elif not evo_instance:
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name",""), step_idx, "failed", "whatsapp", "WhatsApp instance not configured")
                _bump_campaign_stat(campaign["_id"], "failed")
            else:
                result = send_whatsapp_message(evo_instance, phone, message)
                status = "sent" if result["success"] else "failed"
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name",""), step_idx, status, "whatsapp", result.get("error",""))
                _bump_campaign_stat(campaign["_id"], status)

        # ── EMAIL (Gmail or Resend, chosen per step) ──
        elif step_type == "email":
            provider = step.get("provider", "gmail")
            to_addr  = lead.get("email", "")
            subject  = step.get("subject", "")
            body     = step.get("body", "")

            if step.get("use_ai"):
                ai_result = generate_ai_content(lead, "email", step.get("ai_instructions", ""))
                if ai_result.get("success"):
                    subject = ai_result.get("subject", subject)
                    body    = ai_result.get("body", body)

            subject = render_template_vars(subject, lead)
            body    = render_template_vars(body, lead)
            channel = f"email_{provider}"

            if not to_addr:
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name",""), step_idx, "failed", channel, "No email address")
                _bump_campaign_stat(campaign["_id"], "failed")
            elif provider == "resend":
                result = send_resend_email(resend_key, resend_from, to_addr, subject, body)
                status = "sent" if result["success"] else "failed"
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name",""), step_idx, status, channel, result.get("error",""))
                _bump_campaign_stat(campaign["_id"], status)
            else:  # gmail
                result = send_gmail(gmail_addr, gmail_pass, to_addr, subject, body)
                status = "sent" if result["success"] else "failed"
                _log_execution(owner_id, campaign_id, lead_id, lead.get("name",""), step_idx, status, channel, result.get("error",""))
                _bump_campaign_stat(campaign["_id"], status)

        step_idx += 1

    campaigns_col.update_one({"_id": campaign["_id"]}, {"$set": {"last_run_at": datetime.utcnow()}})


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
        for lead in leads:
            execute_campaign_for_lead(campaign, lead, user, owner_id)
            campaigns_col.update_one({"_id": campaign["_id"]}, {"$inc": {"stats.pending": -1}})
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
            "password": generate_password_hash(password),
            "status": "active",
            "email_verified": False,
            "plan": {"name": "Free", "credits": 100},
            "integrations": {},
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
        user = users_col.find_one({"username": username, "type": "user"})
        if not user or not check_password_hash(user["password"], password):
            flash("Invalid username or password"); return redirect("/login")
        session["user_id"] = str(user["_id"])
        session["username"] = user["username"]
        users_col.update_one({"_id": user["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
        return redirect("/dashboard")
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    user = users_col.find_one({"_id": ObjectId(session["user_id"])})
    if not user:
        session.clear(); return redirect("/login")
    return render_template("dashboard.html", user=user)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ==================================================================
# LEADS API  (unchanged)
# ==================================================================

@app.route("/api/leads", methods=["GET"])
@login_required
def api_list_leads():
    q = request.args.get("q", "").strip()
    query = {"owner_id": current_user_id()}
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
    lead["owner_id"]  = current_user_id()
    lead["source"]    = "manual"
    lead["created_at"] = datetime.utcnow()
    lead["updated_at"] = datetime.utcnow()
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
            lead["owner_id"]   = current_user_id()
            lead["source"]     = row.get("source", "manual")
            lead["created_at"] = datetime.utcnow()
            lead["updated_at"] = datetime.utcnow()
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
            "created_at": now, "updated_at": now,
        })
        inserted += 1
    if docs: leads_col.insert_many(docs)
    return jsonify({"inserted": inserted, "skipped": skipped})


@app.route("/api/leads/template", methods=["GET"])
@login_required
def api_leads_template():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(LEAD_TEMPLATE_HEADERS)
    writer.writerow(["Bhuvi Patel","Al Noor Spices Trading LLC","info@alnoorspices.ae","+971 50 123 4567","https://alnoorspices.ae","Importer looking for bulk basmati rice"])
    return Response(buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pravaahai_leads_template.csv"})


# ==================================================================
# CAMPAIGNS API
# ==================================================================

@app.route("/api/campaigns", methods=["GET"])
@login_required
def api_list_campaigns():
    campaigns = list(campaigns_col.find({"owner_id": current_user_id()}).sort("created_at", -1))
    return jsonify({"campaigns": [serialize_campaign(c) for c in campaigns]})


@app.route("/api/campaigns", methods=["POST"])
@login_required
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
def api_get_campaign(campaign_id):
    try: oid = ObjectId(campaign_id)
    except InvalidId: return jsonify({"error": "Invalid campaign id"}), 400
    c = campaigns_col.find_one({"_id": oid, "owner_id": current_user_id()})
    if not c: return jsonify({"error": "Campaign not found"}), 404
    return jsonify({"campaign": serialize_campaign(c)})


@app.route("/api/campaigns/<campaign_id>", methods=["PUT", "PATCH"])
@login_required
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
def api_delete_campaign(campaign_id):
    try: oid = ObjectId(campaign_id)
    except InvalidId: return jsonify({"error": "Invalid campaign id"}), 400
    result = campaigns_col.delete_one({"_id": oid, "owner_id": current_user_id()})
    if result.deleted_count == 0: return jsonify({"error": "Campaign not found"}), 404
    executions_col.delete_many({"campaign_id": campaign_id})
    return jsonify({"deleted": True})


@app.route("/api/campaigns/<campaign_id>/launch", methods=["POST"])
@login_required
def api_launch_campaign(campaign_id):
    try: ObjectId(campaign_id)
    except InvalidId: return jsonify({"error": "Invalid campaign id"}), 400
    ok = launch_campaign(campaign_id, current_user_id())
    if not ok:
        return jsonify({"error": "Could not launch campaign. Check leads are attached and integrations are configured."}), 400
    return jsonify({"launched": True})


@app.route("/api/campaigns/<campaign_id>/logs", methods=["GET"])
@login_required
def api_campaign_logs(campaign_id):
    logs = list(executions_col.find(
        {"campaign_id": campaign_id, "owner_id": current_user_id()}
    ).sort("executed_at", -1).limit(500))
    return jsonify({"logs": [serialize_execution(e) for e in logs]})


# ==================================================================
# INTEGRATIONS / CREDENTIALS API
# ==================================================================


@app.route("/api/integrations", methods=["GET"])
@login_required
def api_get_integrations():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    if not user: return jsonify({"error": "User not found"}), 404
    creds = user.get("integrations", {})
    return jsonify({
        "evo_instance":        creds.get("evo_instance", ""),
        "gmail_address":       creds.get("gmail_address", ""),
        "gmail_app_password":  "●●●●●●●●" if creds.get("gmail_app_password") else "",
        "resend_api_key":      "●●●●●●●●" if creds.get("resend_api_key") else "",
        "resend_from_address": creds.get("resend_from_address", ""),
        "has_evo":     bool(creds.get("evo_instance")),
        "has_gmail":   bool(creds.get("gmail_app_password")),
        "has_resend":  bool(creds.get("resend_api_key")),
    })


@app.route("/api/integrations", methods=["POST"])
@login_required
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

    users_col.update_one({"_id": ObjectId(current_user_id())}, {"$set": {"integrations": existing}})
    return jsonify({"saved": True})


@app.route("/api/integrations/test/whatsapp", methods=["POST"])
@login_required
def api_test_whatsapp():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    result = get_instance_status(creds.get("evo_instance", ""))
    return jsonify(result)


@app.route("/api/integrations/test/resend", methods=["POST"])
@login_required
def api_test_resend():
    user = users_col.find_one({"_id": ObjectId(current_user_id())})
    creds = user.get("integrations", {}) if user else {}
    result = verify_resend_key(creds.get("resend_api_key", ""))
    return jsonify(result)


#Ai button

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

    result = generate_ai_content(lead, content_type, instructions)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "AI generation failed")}), 400

    return jsonify(result)

# ==================================================================
# RUN
# ==================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)