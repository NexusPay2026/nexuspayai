# NexusPay — Operations & Security Runbook

Operational procedures for the unified backend. Pairs with `README.md` (deploy)
and the security findings report. Nothing here is automated — these are the steps
an operator runs by hand.

---

## 1. Seed accounts & passwords

The backend seeds default accounts **only on a fresh database** (when the row
does not already exist). No passwords are hardcoded in source.

| Account | Source of password | Forced rotation | Seeded when |
|---------|--------------------|-----------------|-------------|
| `admin@nexuspayservices.com` | `ADMIN_SEED_PASSWORD` env var | **Yes** (`must_change_password=True`) | Always (random one-time pw if env unset) |
| `demo@nexuspayservices.com` | `DEMO_SEED_PASSWORD` env var | No | **Only if** `DEMO_SEED_PASSWORD` is set |

### Setting them in Render
1. Render dashboard → the web service → **Environment**.
2. Add `ADMIN_SEED_PASSWORD` (strong, ≥16 chars) and, if you want a demo login,
   `DEMO_SEED_PASSWORD`.
3. Redeploy. On a fresh DB the admin is seeded with that password and forced to
   change it on first login.

### If `ADMIN_SEED_PASSWORD` is unset on a fresh DB
A random one-time password is generated and printed **once** to the boot logs:
```
WARNING nexuspay.auth: Seeded admin admin@nexuspayservices.com with a RANDOM one-time password (ADMIN_SEED_PASSWORD not set): <password> — log in and change it immediately.
```
Grab it from Render → **Logs**, log in, complete the forced change, done.

> ⚠️ **Demo login button coupling:** the portal's "Launch demo" button posts a
> hardcoded `Demo2026!`. To keep that button working, set
> `DEMO_SEED_PASSWORD=Demo2026!`. Otherwise the demo account is not created and
> the button will fail — update the button or drop it.

---

## 2. ONE-OFF: rotate the existing live admin password

**Why this is required:** the seed only runs when the admin row does **not**
exist. Your production database already has `admin@nexuspayservices.com` seeded
with the old default `NexusPay2026!` and `must_change_password=False`. The new
code does **not** retroactively fix that row. The old password is public (it was
in `README.md` and git history), so rotate it **immediately**.

### Option A — Render Shell (recommended, deterministic)
Render dashboard → web service → **Shell**, then run (replace the password):

```bash
python - <<'PY'
import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import User
from app.services.auth_service import hash_password

NEW_PASSWORD = "REPLACE-WITH-A-STRONG-PASSWORD"

async def main():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(
            select(User).where(User.email == "admin@nexuspayservices.com")
        )).scalar_one_or_none()
        if not u:
            print("admin not found"); return
        u.password_hash = hash_password(NEW_PASSWORD)
        u.must_change_password = True   # force another change on next login
        await db.commit()
        print("rotated:", u.email, "must_change_password=", u.must_change_password)

asyncio.run(main())
PY
```
This reuses the app's exact PBKDF2 hashing, so the new password works immediately.
Setting `must_change_password=True` means even this interim password must be
changed at next login via the portal.

### Option B — API only (no shell access)
Uses the admin account to reset its own credential:
1. `POST /api/login` with the current/old admin password → copy the `token`.
2. `GET /api/users` (Bearer token) → find `admin@nexuspayservices.com`, copy its `id`.
3. `POST /api/users/{id}/reset-password` (Bearer token) → response returns a
   `temp_password` and sets `must_change_password=True`.
4. Log out, log in with the `temp_password` → the portal forces the change screen
   → set a strong new password.

> Option A is preferred because it does not depend on the old password still
> being valid (an attacker who knew `NexusPay2026!` could have changed it). After
> rotating, check the append-only audit log / login timestamps for unexpected access.

### Verify
- `POST /api/login` with the OLD password now fails (401).
- `POST /api/login` with the new password succeeds.
- Optional: rotate `JWT_SECRET` in Render afterward to invalidate any tokens
  issued while the default password was live (forces everyone to re-login).

---

## 3. Verifying which build is deployed

- **Backend (Render):** `GET /health` → `git.commit_short`. Must be ≥ the commit
  carrying the feature you expect.
- **Frontend (portal):** browser console on load → `NexusPay portal build commit …`,
  or type `__BUILD__`. Requires the Netlify build-commit substitution (see
  `frontend/index.html` `<head>` note) to be wired, else it reports `unknown`.

---

## 4. Incident: suspected credential exposure
1. Rotate the affected account password (section 2).
2. Rotate `JWT_SECRET` in Render (invalidates all existing sessions).
3. Rotate any exposed AI provider / R2 keys in their dashboards (they are only
   stored as Render env vars; no code change needed).
4. Review the append-only `audit_log` table for `upload` / `analyze` / `dedup_hit`
   actions from unexpected actors.
