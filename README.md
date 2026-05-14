# NexusPay Intelligence — Unified Backend API

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        NETLIFY (4 Frontend Sites)                │
│                                                                  │
│  nexuspayservices.com   freeanalysis.nexus..  nexuspayai.com     │
│  ───────────────────   ──────────────────── ──────────────       │
│  Main Website           Landing Page (CTA)   Portal / UI         │
│  - Company info         - Free analysis      - Login/Auth        │
│  - Services             - Lead capture       - Bloomberg UI      │
│  - Contact form         - MS Bookings link   - AI audits         │
│  - Client Portal link   - Portal signup      - Reports/PDF       │
│                                                                  │
│  nexuspaydashboard.netlify.app                                   │
│  ─────────────────────────────                                   │
│  Visitor Tracking Dashboard                                      │
│  - See who visited the website/landing page                      │
│  - Lead analytics (by source, UTM, daily counts)                 │
│  - Recent visitor feed                                           │
└──────────────┬──────────────┬──────────────┬─────────┬───────────┘
               │              │              │         │
               ▼              ▼              ▼         ▼
┌──────────────────────────────────────────────────────────────────┐
│                  RENDER (This Backend)                            │
│                                                                  │
│  FastAPI Unified API  ─────────────────────────────────────────  │
│                                                                  │
│  /api/login, /api/register, /api/me     ← Auth (JWT)             │
│  /api/merchants (CRUD)                  ← Merchant data          │
│  /api/users (admin CRUD)                ← User management        │
│  /api/audit/run                         ← AI extraction          │
│  /api/leads/contact                     ← Website contact form   │
│  /api/audit/intake                      ← Landing page CTA       │
│  /api/storage/upload-url                ← R2 presigned URLs      │
│  /webhook/visitor                       ← Legacy webhook compat  │
│  /health                                ← Health check           │
│                                                                  │
│  AI Providers (server-side, keys in env vars):                   │
│  ├── Anthropic Claude                                            │
│  ├── OpenAI GPT-4o                                               │
│  ├── Google Gemini                                               │
│  └── xAI Grok                                                   │
└───────────────────┬──────────────────────┬───────────────────────┘
                    │                      │
                    ▼                      ▼
        ┌───────────────────┐   ┌────────────────────┐
        │  PostgreSQL        │   │  Cloudflare R2      │
        │  (Render managed)  │   │  (Object Storage)   │
        │                    │   │                     │
        │  - Users           │   │  - Statement PDFs   │
        │  - Merchants       │   │  - Generated reports│
        │  - Visitors/Leads  │   │  - Exports          │
        │  - Audit Jobs      │   │  - Uploaded files    │
        │  - Metadata        │   │                     │
        └────────────────────┘   └─────────────────────┘
```

## What This Replaces

This single backend replaces:
1. **nexuspay-api** (your current Render service) — portal auth + merchant CRUD
2. **nexuspay-webhook** (separate webhook service) — landing page lead capture
3. **Client-side AI calls** — API keys moved from browser localStorage to server env vars
4. **Separate visitor DB** — visitor tracking now shares the same Postgres, same auth

## Deploy to Render (Step by Step)

### Step 1: Push to GitHub

```bash
# Option A: New repo
cd nexuspay-unified
git init
git add .
git commit -m "NexusPay unified backend v4.0"
git remote add origin https://github.com/NexusPay2026/nexuspay-unified.git
git push -u origin main

# Option B: Replace existing repo
# Copy all files into your existing nexuspayai repo and push
```

### Step 2: Deploy on Render

**Option A — Blueprint (recommended):**
1. Go to **render.com** → **New** → **Blueprint**
2. Connect the GitHub repo
3. Render reads `render.yaml` and creates:
   - Web service: `nexuspay-api`
   - Database: `nexuspay-db`
4. Click **Apply**

**Option B — Manual:**
1. **New** → **Web Service** → connect repo
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2`
4. **New** → **PostgreSQL** → create database
5. Copy the Internal Database URL into the web service's `DATABASE_URL` env var

### Step 3: Set Environment Variables in Render Dashboard

Go to your web service → **Environment** → add these:

| Variable | Value | Notes |
|----------|-------|-------|
| `DATABASE_URL` | (auto from blueprint) | Postgres connection string |
| `JWT_SECRET` | (auto-generated) | Or set your own 64+ char string |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Your Claude key |
| `OPENAI_API_KEY` | `sk-...` | Your ChatGPT key |
| `GOOGLE_API_KEY` | `AIza...` | Your Gemini key |
| `GROK_API_KEY` | `xai-...` | Your Grok key |
| `R2_ACCOUNT_ID` | (from Cloudflare) | Optional until R2 setup |
| `R2_ACCESS_KEY_ID` | (from Cloudflare) | Optional until R2 setup |
| `R2_SECRET_ACCESS_KEY` | (from Cloudflare) | Optional until R2 setup |

### Step 4: Verify

Visit: `https://your-service.onrender.com/health`

You should see:
```json
{
  "status": "ok",
  "service": "nexuspay-unified-api",
  "version": "4.0.0",
  "r2_configured": true,
  "ai_providers": {
    "anthropic": true,
    "openai": true,
    "google": true,
    "grok": true
  }
}
```

### Step 5: Update Frontend API_BASE

In your portal `index.html`, update the API base URL:

```javascript
var API_BASE = 'https://your-service.onrender.com';
```

This is already set in your current file as:
```javascript
var API_BASE = 'https://nexuspay-api-ochi.onrender.com';
```

Just update if the Render service URL changes.

### Step 6: Connect Landing Page

In your landing page HTML, point the form action to:
```
POST https://your-service.onrender.com/webhook/visitor
```
or the new endpoint:
```
POST https://your-service.onrender.com/api/audit/intake
```

### Step 7: Connect Website Contact Form

In nexuspayservices.com, point the contact form to:
```
POST https://your-service.onrender.com/api/leads/contact
```

### Step 8: Connect Visitor Dashboard

In nexuspaydashboard.netlify.app, point API calls to:
```javascript
// List all visitors
GET https://your-service.onrender.com/admin/visitors?limit=50&offset=0

// Visitor count
GET https://your-service.onrender.com/admin/visitors/count

// Full dashboard stats (by source, UTM, daily, recent)
GET https://your-service.onrender.com/admin/dashboard/stats
```
All dashboard endpoints require admin/employee JWT auth — same login as the portal.

## Setting Up Cloudflare R2

1. Go to **Cloudflare Dashboard** → **R2 Object Storage**
2. Create a bucket named `nexuspay-storage`
3. Go to **R2** → **Manage R2 API Tokens** → **Create API Token**
4. Copy the Account ID, Access Key ID, and Secret Access Key
5. Add them to Render env vars

Until R2 is configured, the system works fine — files just won't persist to
object storage, and presigned URL endpoints return 503.

## API Route Map

### Public (no auth)
| Method | Path | Source |
|--------|------|--------|
| GET | `/health` | Any |
| POST | `/api/login` | Portal |
| POST | `/api/register` | Portal |
| POST | `/api/change-password` | Portal |
| POST | `/webhook/visitor` | Landing page |
| POST | `/api/leads/contact` | Website |
| POST | `/api/audit/intake` | Landing page CTA |

### Authenticated (JWT required)
| Method | Path | Source |
|--------|------|--------|
| GET | `/api/me` | Portal |
| GET | `/api/merchants` | Portal |
| POST | `/api/merchants` | Portal |
| PUT | `/api/merchants/{id}` | Portal |
| DELETE | `/api/merchants/{id}` | Portal (admin) |
| POST | `/api/audit/run` | Portal |
| GET | `/api/audit/{id}` | Portal |
| POST | `/api/storage/upload-url` | Portal |
| POST | `/api/storage/download-url` | Portal |

### Admin Only
| Method | Path | Source |
|--------|------|--------|
| GET | `/api/users` | Portal |
| POST | `/api/users` | Portal |
| PUT | `/api/users/{id}` | Portal |
| DELETE | `/api/users/{id}` | Portal |
| POST | `/api/users/{id}/reset-password` | Portal |
| GET | `/admin/visitors` | Portal / Dashboard |
| GET | `/admin/visitors/count` | Portal / Dashboard |
| GET | `/admin/dashboard/stats` | Dashboard (aggregated analytics) |

## File Structure

```
nexuspay-unified/
├── app/
│   ├── __init__.py
│   ├── main.py              ← FastAPI app + CORS + router registration
│   ├── config.py            ← All env vars
│   ├── database.py          ← Async Postgres connection
│   ├── models.py            ← SQLAlchemy ORM models (4 tables)
│   ├── schemas.py           ← Pydantic request/response models
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── health.py        ← GET /health
│   │   ├── auth.py          ← login, register, password, /me
│   │   ├── merchants.py     ← merchant CRUD
│   │   ├── users.py         ← admin user management
│   │   ├── visitors.py      ← leads, contact form, webhook, dashboard stats
│   │   ├── audit.py         ← server-side AI audit
│   │   └── storage.py       ← R2 presigned URLs
│   └── services/
│       ├── __init__.py
│       ├── auth_service.py   ← JWT, password hashing, role guards
│       ├── ai_providers.py   ← Claude, GPT, Gemini, Grok orchestration
│       └── r2_storage.py     ← Cloudflare R2 via boto3
├── alembic/
│   ├── env.py                ← Connects to same DB as the app
│   ├── script.py.mako        ← Migration file template
│   └── versions/
│       └── 001_initial.py    ← Creates all 4 tables
├── alembic.ini                ← Alembic config
├── render.yaml                ← Render blueprint (one-click deploy)
├── requirements.txt           ← Python dependencies
├── .env.example               ← Environment variable template
├── .gitignore
└── README.md                  ← This file
```

## Database Migrations (Alembic)

On first deploy, the app auto-creates all tables via `Base.metadata.create_all`.
After that, use Alembic for any schema changes so you don't lose production data.

### How it works

```bash
# First deploy — tables created automatically. Then stamp Alembic so it knows:
alembic stamp 001_initial

# Later, when you change models.py and need to update the live DB:
alembic revision --autogenerate -m "add_new_column"
alembic upgrade head

# On Render, run migrations before the app starts by changing the start command:
#   alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2
```

The initial migration (`001_initial.py`) matches the exact schema in `models.py` — 
4 tables: `users`, `merchants`, `visitors`, `audit_jobs`.

### Files

```
alembic/
├── env.py                    ← Reads DATABASE_URL from app config
├── script.py.mako            ← Template for new migrations
└── versions/
    └── 001_initial.py        ← Creates all 4 tables
alembic.ini                   ← Alembic config (project root)
```

## Default Accounts

On first startup, the backend auto-seeds:

| Email | Password | Role |
|-------|----------|------|
| admin@nexuspayservices.com | NexusPay2026! | admin |
| demo@nexuspayservices.com | Demo2026! | demo |

**Change the admin password immediately after first deploy.**
