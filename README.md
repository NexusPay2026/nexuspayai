# 🐺 NexusPay Intelligence Platform v4.0

**Bloomberg-grade merchant processing command center with tiered SaaS.**

## Project Structure

```
nexuspayai/                  ← your GitHub repo root
├── render.yaml              ← Render reads this for auto-deploy
├── requirements.txt         ← Python dependencies (at root)
├── backend/
│   ├── __init__.py
│   ├── main.py              ← FastAPI application (all endpoints)
│   ├── database.py          ← PostgreSQL connection
│   ├── models.py            ← User, Merchant, AuditLog tables
│   ├── schemas.py           ← Request/response validation
│   ├── auth.py              ← bcrypt + JWT tokens
│   └── requirements.txt     ← (copy, for reference)
├── frontend/
│   └── index.html           ← Full NexusPay UI
├── README.md
└── .gitignore
```

## Quick Start (Local Development)

```bash
# 1. Clone the repo
git clone https://github.com/NexusPay2026/nexuspayai.git
cd nexuspayai

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run (uses SQLite locally by default)
uvicorn backend.main:app --reload --port 8000

# 4. Open browser
# API docs: http://localhost:8000/docs
# Frontend:  http://localhost:8000/
```

## Deploy to Render (Production)

### Blueprint (automatic)
1. Push repo to GitHub
2. Render → New → Blueprint → connect repo
3. Render reads `render.yaml`, creates web service + PostgreSQL
4. Done — API is live

### Manual setup
1. Create PostgreSQL database on Render
2. Create Web Service → Python → connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
5. Set `DATABASE_URL` and `JWT_SECRET` env vars

## Default Credentials

| Email | Password | Role |
|-------|----------|------|
| admin@nexuspayservices.com | NexusPay2026! | Admin |
| demo@nexuspayservices.com | Demo2026! | Demo |

## Contact

- Website: https://www.nexuspayservices.com
- Phone: (720) 689-7272
- Email: admin@nexuspayservices.com

© 2024–2026 NexusPay Services, LLC. All rights reserved.
