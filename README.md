# pravahAI

PravahAI — Multi-channel Conversational AI platform (WhatsApp, Email, Voice, Web)

PravahAI is a flexible, extensible conversational AI platform that helps businesses automate and personalize customer communication across WhatsApp, email, voice, and web channels. It combines modern NLU/LLM capabilities with robust integrations for messaging, telephony, and email delivery to create unified, context-aware conversation flows.

- Languages used in this repository: HTML (frontend), Python (backend)
- Target audiences: product teams, customer support, sales operations, marketing, devops

---

## Table of Contents

- [Vision](#vision)
- [What pravahAI Does](#what-pravahai-does)
- [Why It Matters](#why-it-matters)
- [How It Helps Businesses](#how-it-helps-businesses)
- [Key Features](#key-features)
- [Architecture Overview](#architecture-overview)
- [Tech Stack](#tech-stack)
- [Quickstart (Local)](#quickstart-local)
- [Configuration & Integrations](#configuration--integrations)
  - [WhatsApp](#whatsapp)
  - [Email](#email)
  - [Voice AI](#voice-ai)
- [API & Webhooks](#api--webhooks)
- [Security & Privacy](#security--privacy)
- [Deployment & Scaling](#deployment--scaling)
- [Observability & Analytics](#observability--analytics)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License & Contact](#license--contact)
- [FAQ](#faq)

---

## Vision

Enable businesses to deliver seamless, conversational experiences that feel natural, fast, and consistent across channels. PravahAI aims to reduce resolution time, increase customer satisfaction, and lower operational costs by automating routine interactions while providing escalation paths for humans.

---

## What pravahAI Does

PravahAI provides:

- Multi-channel conversational automation: WhatsApp, email, voice, and web chat.
- Message orchestration: route, transform, and respond across channels while preserving context.
- NLU & LLM integration: intent classification, entity extraction, and generative replies.
- Rich integrations: Twilio/Meta for WhatsApp and voice, SendGrid/SMTP for email, and common CRMs.
- Developer-friendly APIs and webhook-driven event handling.
- Analytics dashboard for routing, resolution rates, and conversation metrics.

---

## Why It Matters

Customers expect fast, accurate responses on the channel they prefer. Building and maintaining separate solutions for each channel is expensive and error-prone. A single platform that unifies message handling, context, and intelligence:

- Improves response time and CSAT (customer satisfaction).
- Reduces agent load and operational cost via automation.
- Keeps customer history and context consistent across channels.
- Enables personalized, proactive communication (e.g., follow-ups, reminders).

---

## How It Helps Businesses

Use cases and benefits:

- Customer Support: Auto-resolve common issues, triage complex ones to agents with full context.
- Sales & Lead Nurturing: Qualify leads via chat or WhatsApp, trigger follow-up emails or calls.
- Notifications & Alerts: Send secure transactional messages (OTP, booking confirmations) via WhatsApp and email.
- Voice-enabled IVR: Intelligent inbound voice routing, transcription, and conversational IVR.
- Omnichannel history: Preserve conversation state across sessions and channels for improved CX.

Business outcomes:
- Faster first response and shorter resolution times.
- Lower cost per contact via automation.
- Higher conversion and retention through timely, personalized outreach.

---

## Key Features

- Channel support: WhatsApp (templates & session messages), Email, Voice (IVR & callbacks), Web chat.
- Context management: session/state store, user profiles, conversation history.
- NLU + Generative responses: mix of intent-driven responses and LLM-powered dynamic replies.
- Rules & workflows: conditional flows, fallback routing, escalation to human agents.
- Template management for WhatsApp and transactional emails.
- Metrics & insights: conversation volumes, success rates, response times.
- Extensible connectors for CRMs, databases, and third-party APIs.

---

## Architecture Overview

High-level components:

1. Frontend: lightweight HTML/JS UI for dashboard, flows, and testing.
2. Backend (Python): API server that:
   - Ingests webhooks (WhatsApp, voice, email),
   - Runs NLU/LLM inference,
   - Executes flow logic,
   - Stores session & message state (DB/Redis),
   - Dispatches outbound messages via channel connectors.
3. Connectors:
   - WhatsApp (e.g., Twilio or Meta Business API)
   - Email (SMTP, SendGrid)
   - Voice (Twilio Voice or SIP integrations)
4. Optional services:
   - Redis for session caching,
   - PostgreSQL for persistent storage,
   - Worker queue (Celery/RQ) for background tasks,
   - Monitoring/metrics stack (Prometheus, Grafana).

Diagram (conceptual):
User <-> Channel Provider (WhatsApp/Phone/Email) <-> PravahAI API (webhooks, NLU, flows) <-> Backend services & DB

---

## Tech Stack

- Frontend: HTML + JS (static UI components)
- Backend: Python (Flask / FastAPI / Django — implementer choice)
- DB: PostgreSQL (recommended)
- Cache/Queue: Redis, Celery/RQ
- Optional: Docker, Kubernetes for deployment
- Integrations: Twilio / Meta / SendGrid / SMTP / Google Cloud Speech or other Speech-to-Text/TTS services
- LLM Providers: OpenAI, local LLMs, or enterprise LLMs as desired

---

## Quickstart (Local)

Prerequisites:
- Python 3.10+
- PostgreSQL or SQLite for quick testing
- Redis (optional, recommended)
- ngrok (for exposing local webhooks during development)

Prepare environment:
1. Clone the repo:
   git clone https://github.com/technosanjivani/pravahAI.git
2. Create virtualenv and install:
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
3. Copy and configure environment variables:
   cp .env.example .env
   Edit .env with your keys (see below)

Run locally:
- For a Flask/FastAPI server:
  export FLASK_APP=app
  flask run --host=0.0.0.0 --port=8000
- Or:
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Expose webhook URL (ngrok):
- ngrok http 8000
- Use the generated https URL for WhatsApp / Twilio webhooks and for Voice callbacks.

Docker (example):
- docker build -t pravahai:local .
- docker run -p 8000:8000 --env-file .env pravahai:local

---

## Configuration & Integrations

Core environment variables (examples):
- APP_ENV=development
- SECRET_KEY=<your-secret>
- DATABASE_URL=postgresql://user:pass@host:5432/dbname
- REDIS_URL=redis://localhost:6379/0
- TWILIO_ACCOUNT_SID=
- TWILIO_AUTH_TOKEN=
- TWILIO_WHATSAPP_NUMBER=whatsapp:+1415xxxxxxx
- SENDGRID_API_KEY=
- SMTP_HOST=, SMTP_PORT=, SMTP_USER=, SMTP_PASS=
- GOOGLE_APPLICATION_CREDENTIALS=/path/to/google-creds.json
- OPENAI_API_KEY= (if using OpenAI)
- WEBHOOK_BASE_URL=https://yourdomain.com

WhatsApp
- Integration via Twilio or Meta Business API.
- Configure webhook endpoints to receive inbound messages.
- Use template messages for notifications (pre-approved templates as required by WhatsApp policies).
- Example inbound webhook path: POST /webhooks/whatsapp
- Example outbound via Twilio:
  client.messages.create(
    from_=TWILIO_WHATSAPP_NUMBER,
    body="Hello from PravahAI",
    to="whatsapp:+91XXXXXXXXXX"
  )

Email
- Send via SMTP or transactional providers (SendGrid, SES).
- Template engine for formatting transactional emails.
- Webhook endpoint for inbound/relay emails (if using SendGrid inbound parse).

Voice AI
- Use Twilio Voice webhooks to receive call events and media; integrate with STT/TTS.
- Example flow: inbound call -> Twilio webhook -> PravahAI interprets caller intent via STT -> responds using TTS or routes to agent.
- Use Google Speech-to-Text or other provider for accurate transcription, and a TTS provider (Google, Amazon Polly) for voice responses.

---

## API & Webhooks

- POST /api/messages/inbound — generic inbound message handler
- POST /webhooks/whatsapp — WhatsApp inbound events
- POST /webhooks/voice — Voice events (call start, transcription)
- POST /webhooks/email — Inbound email or bounce events
- GET /api/sessions/{session_id} — fetch session context
- POST /api/send — send message via specified channel (channel control: whatsapp/email/voice)

Example: Send a WhatsApp message via API
curl -X POST https://api.example.com/api/send \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"channel":"whatsapp","to":"+91XXXXXXXXXX","body":"Your verification code is 123456"}'

Webhook security:
- Validate webhook signatures from providers (Twilio X-Twilio-Signature, SendGrid X-Twilio-Email-Event-Webhook-Signature or similar).
- Use HTTPS and rotate secrets regularly.

---

## Security & Privacy

- Data minimization: only store conversation data required for context and compliance.
- Encryption: use TLS for all transport; encrypt sensitive data at rest where applicable.
- Access control: role-based access for dashboards and agent tools.
- Compliance: support for region-specific data residency and GDPR considerations — provide opt-out and data deletion flows.
- Template approvals: WhatsApp templates should be used for out-of-session notifications to comply with WhatsApp Business policies.

---

## Deployment & Scaling

- Containerize the app and run on Kubernetes for production-grade scaling.
- Use managed databases (AWS RDS / Cloud SQL) and Redis (ElastiCache / Memorystore).
- Autoscale workers for background tasks based on queue depth.
- For high throughput messaging, batch and queue outbound messages; respect provider rate limits.
- Use CDNs and caching for frontend assets.

---

## Observability & Analytics

- Track: inbound/outbound volumes, success/failure rates, avg response times, escalate rates.
- Instrument: Prometheus metrics and Grafana dashboards.
- Logging: structured logs, correlation IDs for tracing a conversation end-to-end.
- Conversation analytics: identify failed intents, common escalations, and templates that drive best outcomes.

---

## Roadmap

Planned improvements:
- Richer visual flow builder for non-developers.
- Native agent workspace for live chat handoff.
- More connectors (WhatsApp direct Meta API, WhatsApp Cloud API improvements).
- Built-in privacy controls and audit logs.
- Support for on-premise LLMs and higher privacy modes.

---

## Contributing

We welcome contributions. Suggested workflow:
1. Fork the repo.
2. Create a feature branch (feature/your-feature).
3. Add tests and documentation.
4. Open a PR with a clear description and screenshots if applicable.

Please follow repository coding standards and include unit/integration tests for critical flows.

---

## License & Contact

- License: MIT (or update to your preferred license)
- Contact / maintainer: technosanjivani / pravahAI (GitHub)
- For enterprise or professional support, reach out via repo maintainer contact details.

---

## FAQ

Q: Can pravahAI be used without third-party paid services?
A: You can run core components locally, but for production you typically need provider accounts (Twilio, SendGrid, cloud STT/TTS). Some parts (basic NLU) can run locally if using open-source models.

Q: Is WhatsApp supported worldwide?
A: WhatsApp availability and capabilities depend on provider (Twilio/Meta) and local regulations. Template approvals are required for outbound notifications.

Q: How do I handle fallbacks to human agents?
A: Define flow conditions to escalate, and forward session context to an agent UI or a third-party helpdesk via connector.

---

Thank you for using pravahAI — a unified platform to modernize and automate customer conversations across channels.
