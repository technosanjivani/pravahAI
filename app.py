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

import pandas as pd

# ----------------------------------
# Load Environment Variables
# ----------------------------------

load_dotenv()

# ----------------------------------
# Flask App
# ----------------------------------

app = Flask(__name__)

app.secret_key = os.getenv(
    "SECRET_KEY",
    "change-me"
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Max upload size for CSV/Excel imports (5 MB)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# ----------------------------------
# MongoDB
# ----------------------------------

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://localhost:27017/"
)

client = MongoClient(MONGO_URI)

db = client["pravahai"]

users_col = db["pravah-users"]
leads_col = db["pravah-leads"]

# ----------------------------------
# Create Indexes
# ----------------------------------

try:
    users_col.create_index(
        "username",
        unique=True
    )

    users_col.create_index(
        "email",
        unique=True
    )

    leads_col.create_index("owner_id")
    leads_col.create_index([("owner_id", 1), ("created_at", -1)])

except Exception:
    pass

# ----------------------------------
# Lead Import Settings
# ----------------------------------

ALLOWED_IMPORT_EXTENSIONS = {"csv", "xlsx", "xls"}

# Canonical lead headers (used for the table, exports & the
# downloadable import template). Keep this list as the single
# source of truth for what a "lead" looks like.
LEAD_TEMPLATE_HEADERS = [
    "Name",
    "Business Name",
    "Email",
    "Phone",
    "Website",
    "Description",
]

# Maps many possible incoming column-name variants (lower-cased,
# stripped of spaces/underscores) -> our internal field name.
IMPORT_HEADER_ALIASES = {
    "name": "name",
    "leadname": "name",
    "fullname": "name",
    "contactname": "name",

    "businessname": "business_name",
    "business": "business_name",
    "company": "business_name",
    "companyname": "business_name",

    "email": "email",
    "emailaddress": "email",

    "phone": "phone",
    "phonenumber": "phone",
    "number": "phone",
    "mobile": "phone",
    "contactnumber": "phone",

    "website": "website",
    "url": "website",
    "site": "website",

    "description": "description",
    "notes": "description",
    "note": "description",
    "details": "description",
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
    """Pull out only the fields we accept from a lead JSON payload."""
    return {
        "name": (data.get("name") or "").strip(),
        "business_name": (data.get("business_name") or "").strip(),
        "email": (data.get("email") or "").strip().lower(),
        "phone": (data.get("phone") or "").strip(),
        "website": (data.get("website") or "").strip(),
        "description": (data.get("description") or "").strip(),
    }


# ----------------------------------
# Home
# ----------------------------------

@app.route("/")
def home():

    if "user_id" in session:
        return redirect("/dashboard")

    return redirect("/login")

# ----------------------------------
# Signup
# ----------------------------------

@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip().lower()

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        phone = request.form.get(
            "phone",
            ""
        ).strip()

        business_name = request.form.get(
            "business_name",
            ""
        ).strip()

        business_type = request.form.get(
            "business_type",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if not username:
            flash("Username required")
            return redirect("/signup")

        if not password:
            flash("Password required")
            return redirect("/signup")

        existing_user = users_col.find_one({
            "username": username,
            "type": "user"
        })

        if existing_user:
            flash("Username already exists")
            return redirect("/signup")

        existing_email = users_col.find_one({
            "email": email,
            "type": "user"
        })

        if existing_email:
            flash("Email already exists")
            return redirect("/signup")

        hashed_password = generate_password_hash(
            password
        )

        user_data = {

            "type": "user",

            "username": username,
            "email": email,
            "phone": phone,

            "business_name": business_name,
            "business_type": business_type,

            "password": hashed_password,

            "status": "active",

            "email_verified": False,

            "plan": {
                "name": "Free",
                "credits": 100
            },

            "created_at": datetime.utcnow(),
            "last_login": None
        }

        users_col.insert_one(user_data)

        flash("Account created successfully")

        return redirect("/login")

    return render_template("signup.html")

# ----------------------------------
# Login
# ----------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        user = users_col.find_one({
            "username": username,
            "type": "user"
        })

        if not user:

            flash("Invalid username or password")
            return redirect("/login")

        if not check_password_hash(
            user["password"],
            password
        ):

            flash("Invalid username or password")
            return redirect("/login")

        session["user_id"] = str(
            user["_id"]
        )

        session["username"] = user["username"]

        users_col.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "last_login": datetime.utcnow()
                }
            }
        )

        return redirect("/dashboard")

    return render_template("login.html")

# ----------------------------------
# Dashboard
# ----------------------------------

@app.route("/dashboard")
@login_required
def dashboard():

    user = users_col.find_one({
        "_id": ObjectId(
            session["user_id"]
        )
    })

    if not user:

        session.clear()
        return redirect("/login")

    return render_template(
        "dashboard.html",
        user=user
    )

# ----------------------------------
# Logout
# ----------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


# ==========================================================
# LEADS API
# ==========================================================

# ----------------------------------
# List leads (with optional ?q= search)
# ----------------------------------

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

    leads = list(
        leads_col.find(query).sort("created_at", -1)
    )

    return jsonify({
        "leads": [serialize_lead(l) for l in leads]
    })


# ----------------------------------
# Create a single lead
# ----------------------------------

@app.route("/api/leads", methods=["POST"])
@login_required
def api_create_lead():

    data = request.get_json(silent=True) or {}

    lead = clean_lead_payload(data)

    if not lead["name"]:
        return jsonify({"error": "Name is required"}), 400

    lead["owner_id"] = current_user_id()
    lead["source"] = "manual"
    lead["created_at"] = datetime.utcnow()
    lead["updated_at"] = datetime.utcnow()

    result = leads_col.insert_one(lead)
    saved = leads_col.find_one({"_id": result.inserted_id})

    return jsonify({"lead": serialize_lead(saved)}), 201


# ----------------------------------
# Bulk create / update (used by the
# spreadsheet "Save All" action)
# ----------------------------------

@app.route("/api/leads/bulk", methods=["POST"])
@login_required
def api_bulk_save_leads():

    data = request.get_json(silent=True) or {}
    rows = data.get("leads", [])

    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "No leads provided"}), 400

    saved_leads = []
    skipped = 0

    for row in rows:

        lead = clean_lead_payload(row)

        if not lead["name"]:
            skipped += 1
            continue

        lead_id = row.get("_id")

        if lead_id:
            # Update existing lead (only if it belongs to this user)
            try:
                oid = ObjectId(lead_id)
            except InvalidId:
                skipped += 1
                continue

            lead["updated_at"] = datetime.utcnow()

            leads_col.update_one(
                {"_id": oid, "owner_id": current_user_id()},
                {"$set": lead}
            )

            updated = leads_col.find_one(
                {"_id": oid, "owner_id": current_user_id()}
            )

            if updated:
                saved_leads.append(serialize_lead(updated))
            else:
                skipped += 1

        else:
            # Insert new lead
            lead["owner_id"] = current_user_id()
            lead["source"] = row.get("source", "manual")
            lead["created_at"] = datetime.utcnow()
            lead["updated_at"] = datetime.utcnow()

            result = leads_col.insert_one(lead)
            created = leads_col.find_one({"_id": result.inserted_id})
            saved_leads.append(serialize_lead(created))

    return jsonify({
        "leads": saved_leads,
        "saved": len(saved_leads),
        "skipped": skipped
    })


# ----------------------------------
# Update a single lead
# ----------------------------------

@app.route("/api/leads/<lead_id>", methods=["PUT", "PATCH"])
@login_required
def api_update_lead(lead_id):

    try:
        oid = ObjectId(lead_id)
    except InvalidId:
        return jsonify({"error": "Invalid lead id"}), 400

    data = request.get_json(silent=True) or {}
    lead = clean_lead_payload(data)

    if not lead["name"]:
        return jsonify({"error": "Name is required"}), 400

    lead["updated_at"] = datetime.utcnow()

    result = leads_col.update_one(
        {"_id": oid, "owner_id": current_user_id()},
        {"$set": lead}
    )

    if result.matched_count == 0:
        return jsonify({"error": "Lead not found"}), 404

    updated = leads_col.find_one({"_id": oid})

    return jsonify({"lead": serialize_lead(updated)})


# ----------------------------------
# Delete a single lead
# ----------------------------------

@app.route("/api/leads/<lead_id>", methods=["DELETE"])
@login_required
def api_delete_lead(lead_id):

    try:
        oid = ObjectId(lead_id)
    except InvalidId:
        return jsonify({"error": "Invalid lead id"}), 400

    result = leads_col.delete_one(
        {"_id": oid, "owner_id": current_user_id()}
    )

    if result.deleted_count == 0:
        return jsonify({"error": "Lead not found"}), 404

    return jsonify({"deleted": True})


# ----------------------------------
# Import leads from CSV / Excel
# ----------------------------------

@app.route("/api/leads/import", methods=["POST"])
@login_required
def api_import_leads():

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]

    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        return jsonify({
            "error": "Unsupported file type. Please upload a .csv, .xlsx or .xls file."
        }), 400

    try:
        if ext == "csv":
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    # Map incoming columns -> our internal field names
    column_map = {}
    for col in df.columns:
        key = normalize_header(col)
        if key in IMPORT_HEADER_ALIASES:
            column_map[col] = IMPORT_HEADER_ALIASES[key]

    if "name" not in column_map.values():
        return jsonify({
            "error": (
                "Could not find a 'Name' column. Expected headers like: "
                + ", ".join(LEAD_TEMPLATE_HEADERS)
            )
        }), 400

    df = df.rename(columns=column_map)

    inserted = 0
    skipped = 0
    now = datetime.utcnow()
    docs = []

    for _, row in df.iterrows():

        name = str(row.get("name", "") or "").strip()

        if not name or name.lower() == "nan":
            skipped += 1
            continue

        def clean(field):
            val = row.get(field, "")
            if pd.isna(val):
                return ""
            return str(val).strip()

        docs.append({
            "owner_id": current_user_id(),
            "name": name,
            "business_name": clean("business_name"),
            "email": clean("email").lower(),
            "phone": clean("phone"),
            "website": clean("website"),
            "description": clean("description"),
            "source": "import",
            "created_at": now,
            "updated_at": now,
        })

        inserted += 1

    if docs:
        leads_col.insert_many(docs)

    return jsonify({
        "inserted": inserted,
        "skipped": skipped
    })


# ----------------------------------
# Downloadable CSV import template
# ----------------------------------

@app.route("/api/leads/template", methods=["GET"])
@login_required
def api_leads_template():

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(LEAD_TEMPLATE_HEADERS)
    writer.writerow([
        "Bhuvi Patel",
        "Al Noor Spices Trading LLC",
        "info@alnoorspices.ae",
        "+971 50 123 4567",
        "https://alnoorspices.ae",
        "Importer looking for bulk basmati rice and spices"
    ])

    output = buffer.getvalue()

    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=pravaahai_leads_template.csv"
        }
    )


# ----------------------------------
# Run App
# ----------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )