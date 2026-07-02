PravaahAI – Technical Documentation
Project Overview
PravaahAI is a multi-channel automation platform for managing leads and executing sophisticated marketing campaigns via WhatsApp, Email (Gmail & Resend), with conditional logic and scheduled delays. Built with Flask + MongoDB, it enables users to create no-code campaign workflows, import lead lists, and track execution metrics across channels.

Stack
Languages: Python (34.3%), HTML (65.5%), CSS (0.2%)
Framework: Flask 2.3.0
Database: MongoDB
Email Services: Gmail SMTP, Resend API
Messaging: Evolution API (WhatsApp)
Authentication: Session-based with password hashing (werkzeug)
Data Processing: Pandas (CSV/Excel imports)
Repository Structure
Code
pravahAI/
├── app.py                      # Main Flask application (29.4 KB)
├── evo.py                      # Evolution API (WhatsApp) integration
├── gmail.py                    # Gmail SMTP email sender
├── resend.py                   # Resend API email sender
├── scrap.py                    # Experimental web scraping script
├── requirements.txt            # Python dependencies
├── templates/
│   ├── login.html              # Login page
│   ├── signup.html             # User registration
│   └── dashboard.html          # Main application interface (68.4 KB)
└── static/
    ├── css/
    │   └── style.css           # Embedded in dashboard.html
    └── img/
        └── PravaahAI-logo.png  # Logo asset
Database Schema
Collections
1. pravah-users
Stores user accounts and integration credentials.

JavaScript
{
  _id: ObjectId,
  type: "user",
  username: String (unique),
  email: String (unique),
  phone: String,
  business_name: String,
  business_type: String,
  password: String (hashed),
  status: "active",
  email_verified: Boolean,
  plan: { name: String, credits: Number },
  integrations: {
    evo_instance: String,
    evo_url: String,
    evo_key: String (masked in API),
    gmail_address: String,
    gmail_app_password: String (masked),
    resend_api_key: String (masked),
    resend_from_address: String
  },
  created_at: DateTime,
  last_login: DateTime
}
Indexes:

username (unique)
email (unique)
2. pravah-leads
Contact records for campaign targeting.

JavaScript
{
  _id: ObjectId,
  owner_id: String (user ObjectId as string),
  name: String (required),
  business_name: String,
  email: String (lowercased),
  phone: String (E.164 format for WhatsApp),
  website: String,
  description: String,
  source: "manual" | "import",
  created_at: DateTime,
  updated_at: DateTime
}
Indexes:

owner_id (single field)
owner_id + created_at (compound, descending by date)
Template Variables: Campaigns can reference {{name}}, {{email}}, {{phone}}, {{business_name}}, {{website}}, {{description}} in messages.

3. pravah-campaigns
Campaign definitions with execution flow.

JavaScript
{
  _id: ObjectId,
  owner_id: String,
  name: String,
  description: String,
  status: "draft" | "active" | "running" | "paused" | "completed",
  flow: Array<Step>,           // Steps array (see Flow Step Schema)
  lead_ids: Array<String>,     // Lead ObjectIds as strings
  stats: { sent: Number, failed: Number, pending: Number },
  created_at: DateTime,
  updated_at: DateTime,
  last_run_at: DateTime
}
Indexes:

owner_id
Flow Step Schema: Each step in the flow array has this structure:

JavaScript
{
  type: "whatsapp" | "email_gmail" | "email_resend" | "wait" | "condition",
  
  // WHATSAPP STEP
  message: String,
  
  // EMAIL STEPS (Gmail & Resend)
  subject: String,
  body: String (HTML),
  
  // WAIT STEP
  unit: "seconds" | "minutes" | "hours" | "days",
  amount: Number,
  
  // CONDITION STEP
  field: String,              // Lead field to check
  operator: "exists" | "not_exists" | "equals" | "contains" | "not_contains",
  value: String,              // Value to compare
  then_step: Number,          // Index of step to jump to if matched
  else_step: Number           // Index of step to jump to if not matched
}
4. pravah-executions
Execution logs per lead per campaign.

JavaScript
{
  _id: ObjectId,
  owner_id: String,
  campaign_id: String,
  lead_id: String,
  lead_name: String,
  step_index: Number,         // Which step in the flow
  status: "sent" | "failed" | "pending",
  channel: "whatsapp" | "email_gmail" | "email_resend",
  error: String,
  executed_at: DateTime
}
Indexes:

campaign_id + lead_id (compound)
owner_id
Core Routes
Authentication
GET/POST /
Home page. Redirects to /dashboard if logged in, otherwise to /login.

GET/POST /signup
User registration.

POST Params: username, email, phone, business_name, business_type, password
Validation: Username and email uniqueness
Response: Redirect to /login on success, flash message on error
Default Plan: Free tier with 100 credits
GET/POST /login
User authentication.

POST Params: username, password
Session: Sets user_id and username in Flask session
Response: Redirect to /dashboard on success
GET /logout
Clears session and redirects to /login.

GET /dashboard
Protected page (requires login). Renders the main application interface.

Leads API
All endpoints require authentication (@login_required).

GET /api/leads
Fetch user's leads with optional search.

Query Params: q (search string, searches name/business/email/phone via regex)
Response: { leads: Array<SerializedLead> }
Sorting: By created_at, descending
Serialized Lead:

JSON
{
  "_id": "string (ObjectId)",
  "name": "string",
  "business_name": "string",
  "email": "string",
  "phone": "string",
  "website": "string",
  "description": "string",
  "source": "manual|import",
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
POST /api/leads
Create a single lead.

Body: { name, business_name, email, phone, website, description }
Validation: Name is required
Response: { lead: SerializedLead } (201 Created)
POST /api/leads/bulk
Create/update multiple leads in one request.

Body: { leads: Array<LeadObject> }
Logic:
If _id provided, updates existing lead
If no _id, creates new lead
Skips leads without a name
Response: { leads: Array, saved: Number, skipped: Number }
PUT/PATCH /api/leads/<lead_id>
Update a lead.

Body: { name, business_name, email, phone, website, description }
Response: { lead: SerializedLead }
DELETE /api/leads/<lead_id>
Delete a lead.

Response: { deleted: true }
POST /api/leads/import
Bulk import leads from CSV/XLSX/XLS file.

Multipart Form: file (CSV, XLSX, or XLS)
Supported Headers: Name, Business Name, Email, Phone, Website, Description
Header Aliases: Flexible matching (e.g., "Full Name" → "Name", "Contact Number" → "Phone")
Response: { inserted: Number, skipped: Number }
File Size Limit: 5 MB (set via MAX_CONTENT_LENGTH)
GET /api/leads/template
Download a CSV template with example data.

Response: CSV file (Content-Type: text/csv)
Campaigns API
All endpoints require authentication.

GET /api/campaigns
List all user's campaigns.

Response: { campaigns: Array<SerializedCampaign> }
Sorting: By created_at, descending
Serialized Campaign:

JSON
{
  "_id": "string",
  "name": "string",
  "description": "string",
  "status": "draft|active|paused|completed|running",
  "flow": Array<Step>,
  "lead_ids": Array<string>,
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "last_run_at": "ISO8601 or null",
  "stats": { "sent": number, "failed": number, "pending": number }
}
POST /api/campaigns
Create a new campaign (defaults to "draft" status).

Body: { name, description?, flow?, lead_ids? }
Response: { campaign: SerializedCampaign } (201)
GET /api/campaigns/<campaign_id>
Fetch a single campaign.

Response: { campaign: SerializedCampaign }
PUT/PATCH /api/campaigns/<campaign_id>
Update campaign metadata or flow.

Body: { name?, description?, flow?, lead_ids?, status? }
Valid Statuses: draft, active, paused, completed
Response: { campaign: SerializedCampaign }
DELETE /api/campaigns/<campaign_id>
Delete campaign and all its execution logs.

Response: { deleted: true }
POST /api/campaigns/<campaign_id>/launch
Start campaign execution (runs in background thread).

Preconditions: Campaign must have leads attached and integrations configured
Response: { launched: true } or error
Async Behavior:
Campaign status set to "running"
Each lead is processed sequentially through the flow
Campaign status changes to "completed" when all leads finish
Execution logs are recorded in pravah-executions
GET /api/campaigns/<campaign_id>/logs
Fetch execution logs for a campaign.

Response: { logs: Array<SerializedExecution> }
Limit: Last 500 logs
Sorting: By executed_at, descending
Serialized Execution:

JSON
{
  "_id": "string",
  "campaign_id": "string",
  "lead_id": "string",
  "lead_name": "string",
  "step_index": number,
  "status": "sent|failed|pending",
  "channel": "whatsapp|email_gmail|email_resend",
  "error": "string (empty if success)",
  "executed_at": "ISO8601"
}
Integrations API
GET /api/integrations
Retrieve user's integration config (secrets masked).

Response:
JSON
{
  "evo_instance": "string",
  "evo_url": "string",
  "evo_key": "●●●●●●●●" (if set),
  "gmail_address": "string",
  "gmail_app_password": "●●●●●●●●" (if set),
  "resend_api_key": "●●●●●●●●" (if set),
  "resend_from_address": "string",
  "has_evo": boolean,
  "has_gmail": boolean,
  "has_resend": boolean
}
POST /api/integrations
Save/update integration credentials.

Body: Any of the above fields
Masking Logic: If a value is already set and the incoming value is exactly "●●●●●●●●", the old value is preserved
Response: { saved: true }
POST /api/integrations/test/whatsapp
Test Evolution API connection.

Response: { success: boolean, state: string?, data: object?, error: string? }
POST /api/integrations/test/resend
Test Resend API key validity.

Response: { success: boolean, error: string? }
Campaign Execution Engine
Flow Execution Logic
The execute_campaign_for_lead(campaign, lead, user, owner_id) function runs in a background thread and processes a campaign's flow sequentially for one lead.

Flow Step Types
1. WAIT Step

Python
{
  "type": "wait",
  "unit": "seconds|minutes|hours|days",
  "amount": 1
}
Blocks execution for the specified duration
Uses time.sleep()
2. CONDITION Step

Python
{
  "type": "condition",
  "field": "name|business_name|email|phone|website|description",
  "operator": "exists|not_exists|equals|contains|not_contains",
  "value": "string",
  "then_step": 2,  // Index to jump to if matched
  "else_step": 4   // Index to jump to if not matched
}
Checks lead field value against operator
Supports case-insensitive comparisons
Branches to different steps based on result
Operators:

exists: Lead field is truthy
not_exists: Lead field is falsy
equals: Exact match (case-insensitive)
contains: Substring match (case-insensitive)
not_contains: Inverse substring match
3. WHATSAPP Step

Python
{
  "type": "whatsapp",
  "message": "Hi {{name}}, check out {{website}}"
}
Sends WhatsApp text message via Evolution API
Template variables replaced with lead data
Logs execution status (sent/failed)
Requires lead's phone number
4. EMAIL_GMAIL Step

Python
{
  "type": "email_gmail",
  "subject": "Hello {{name}}",
  "body": "<h1>Welcome</h1>"
}
Sends email via Gmail SMTP
Supports HTML body with plain-text fallback
Template variables replaced with lead data
5. EMAIL_RESEND Step

Python
{
  "type": "email_resend",
  "subject": "Hello {{name}}",
  "body": "<h1>Welcome</h1>"
}
Sends email via Resend API
Same template and HTML support as Gmail step
Execution Flow Diagram
Code
Launch Campaign
  ↓
For each lead in lead_ids:
  ↓
  step_idx = 0
  While step_idx < flow.length:
    ↓
    Get step at step_idx
    ↓
    if WAIT → sleep(duration) → step_idx++
    if CONDITION → evaluate condition → jump to then_step or else_step
    if MESSAGE → send (whatsapp/email) → log execution → step_idx++
    ↓
  Campaign marked as "completed"
Execution Logging
Every step execution logs to pravah-executions with:

Campaign & lead IDs
Step index, channel, status, error message
Timestamp
Integration Modules
evo.py – Evolution API (WhatsApp)
Function: send_whatsapp_message(instance_name, api_url, api_key, phone_number, message)

Endpoint: POST {api_url}/message/sendText/{instance_name}

Payload:

JSON
{
  "number": "+971501234567",
  "options": {
    "delay": 1000,
    "presence": "composing"
  },
  "textMessage": {
    "text": "Your message"
  }
}
Returns: { success, data, error }

Error Handling:

HTTP errors: Extract message from response JSON or fallback to exception string
Connection errors: Network/timeout errors logged as connection failures
Status Check: get_instance_status(instance_name, api_url, api_key)

Endpoint: GET {api_url}/instance/connectionState/{instance_name}
Returns: { success, state, data, error }
gmail.py – Gmail SMTP
Function: send_gmail(gmail_address, app_password, to_address, subject, body_html, body_text="", from_name="")

Configuration:

SMTP Host: smtp.gmail.com
Port: 587 (STARTTLS)
Authentication: Gmail address + 16-character App Password (spaces removed)
Email Structure:

Multipart message with both plain-text and HTML versions
From header: {from_name} <{gmail_address}>
Returns: { success, error }

Error Handling:

Authentication failures: Special message about App Password vs. regular password
SMTP errors: Logged and returned as strings
HTML Stripping: _strip_html() provides plain-text fallback by:

Converting <br> tags to newlines
Stripping all other HTML tags
resend.py – Resend API
Function: send_resend_email(api_key, from_address, to_address, subject, body_html, body_text="", reply_to="")

Endpoint: POST https://api.resend.com/emails

Payload:

JSON
{
  "from": "sender@example.com",
  "to": ["recipient@example.com"],
  "subject": "Hello",
  "html": "<h1>Content</h1>",
  "text": "Plain text version (optional)",
  "reply_to": "reply@example.com (optional)"
}
Headers: Authorization: Bearer {api_key}

Returns: { success, id, error }

id: Email ID from Resend on success
Validation: verify_resend_key(api_key)

Endpoint: GET https://api.resend.com/domains
Returns: { success, error }
Checks for 401 (invalid key) or other HTTP errors
Frontend (dashboard.html)
Architecture
The dashboard is a single-page application (SPA) with:

Multiple pages: Leads, Campaigns, Flow Builder, Settings
Page switching: showPage() function toggles visibility
Real-time sync: Fetch/save via API calls
Drag-and-drop: Flow builder uses HTML5 drag-and-drop API
Key Sections
1. Sidebar Navigation
Navigation between Leads, Campaigns, Settings
User profile with logout
Responsive toggle on mobile (< 900px)
2. Leads Page
Sheet Editor: Editable table with inline cell editing
Dirty tracking: UI marks unsaved changes
Bulk operations: Import CSV/Excel, add rows, save all
Features:
Live search in global search bar
Template download
Inline editing with validation
Drag-select for multi-row operations
3. Campaigns Page
Grid of campaign cards with status badges
Click to open Flow Builder
Edit/delete/launch controls
4. Flow Builder
Drag-and-drop interface:
Left panel: Step type palette
Center canvas: Drop area for steps
Step types:
WhatsApp (💬)
Email Gmail (📧)
Email Resend (✉️)
Wait (⏳)
Condition (🔀)
Features:
Reorder steps by dragging
Delete steps
Inline field editing with validation
Variable hints for template replacement
Lead assignment modal: Multi-select leads to target
5. Settings Page
Integration configuration forms
Test buttons for WhatsApp and Resend
Masked secret display
Real-time validation
CSS Custom Properties
CSS
--bg-base: #0b0f1a           /* Page background */
--bg-surface: #111827        /* Sidebar/topbar background */
--bg-card: #141c2e           /* Card background */
--bg-hover: #1a2540          /* Hover state */
--accent-1: #00d4ff          /* Primary accent (cyan) */
--accent-2: #0088ff          /* Secondary accent (blue) */
--text-h: #f0f4ff            /* Heading text */
--text-b: #94a3b8            /* Body text */
--text-dim: #4b5a74          /* Dimmed text */
Responsive Design
Mobile (< 560px): Single-column grids
Tablet (< 900px):
Hamburger menu sidebar
Flow builder stacked layout
2-column settings grid → 1-column
Desktop: Full layout with fixed sidebar
Security Considerations
Session Security:

SESSION_COOKIE_HTTPONLY = True (prevents JavaScript access)
SESSION_COOKIE_SAMESITE = "Lax" (CSRF protection)
secret_key from environment variables
Password Hashing:

Werkzeug's generate_password_hash (PBKDF2)
Password verification on login
Credential Masking:

API returns masked secrets (●●●●●●●●)
Masked values preserved on update (only non-masked values overwrite)
Ownership Validation:

All queries filtered by owner_id to prevent cross-user access
User authentication required on protected routes
File Upload:

Restricted to .csv, .xlsx, .xls extensions
5 MB file size limit
Pandas validates file format before processing
Configuration & Environment
Required Environment Variables
env
SECRET_KEY=your-secret-key        # Flask session secret
MONGO_URI=mongodb://localhost:27017/  # MongoDB connection
Optional Integration Credentials
Stored in user document, not env variables:

evo_instance, evo_url, evo_key – Evolution API
gmail_address, gmail_app_password – Gmail
resend_api_key, resend_from_address – Resend
Default Values
Python
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
app.run(host="0.0.0.0", port=5000, debug=True)
How to Run
Prerequisites
Python 3.8+
MongoDB (local or remote)
pip
Setup
bash
# Clone repository
git clone https://github.com/technosanjivani/pravahAI.git
cd pravahAI

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export MONGO_URI="mongodb://localhost:27017/"
export SECRET_KEY="your-secret-key"

# Run application
python app.py
Access
URL: http://localhost:5000
Default Pages:
/signup – Register new account
/login – Sign in
/dashboard – Main application
File Dependencies
Module	Depends On	Purpose
app.py	Flask, MongoDB, all sender modules	Main application logic
evo.py	requests	WhatsApp via Evolution API
gmail.py	smtplib, email.mime	Gmail SMTP integration
resend.py	requests	Resend email API
scrap.py	playwright	(Experimental, unused)
dashboard.html	jQuery, Flask url_for	Frontend SPA
Testing & Debugging
Test WhatsApp Connection
bash
POST /api/integrations/test/whatsapp
# Returns: { "success": true/false, "state": "connected|...", "error": "..." }
Test Resend API
bash
POST /api/integrations/test/resend
# Returns: { "success": true/false, "error": "..." }
Import CSV Template
bash
GET /api/leads/template
# Downloads CSV with column headers and example row
View Campaign Logs
bash
GET /api/campaigns/{campaign_id}/logs
# Returns execution logs for all leads in campaign