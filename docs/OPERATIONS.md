# NexusPay Intelligence — Operations Runbook

> **Hand-off document.** This is what lets another engineer or collaborator
> operate, deploy, and troubleshoot the platform. Keep it current.
>
> **No secrets in this file.** Credentials, API keys, and connection strings
> live only in the Render dashboard environment variables — never in the repo,
> never in this document.

**Last updated:** 2026-06-22 (later session)

---

## 1. System architecture (what runs where)

| Layer | Service | Notes |
|---|---|---|
| Backend API | Render — unified FastAPI service | Deploys from `main`. Auto-runs `alembic upgrade head` on start. |
| Database | Render PostgreSQL | Migrations chain `001 → 005`. |
| Object storage | Cloudflare R2 | Statement file persistence. |
| Portal frontend | Netlify — `nexuspayai.com` | **Manual deploy** (drag-drop or `netlify deploy`). Not git-connected. |
| Other frontends | Netlify — separate sites/repos | paycalculator, freeanalysis, dashboard, matrix, main site. |

All frontends call the single Render backend. The portal contains the
per-provider results UI.

### Verifying which build is live
- **Backend:** `GET /health` → `git.commit_short` field.
- **Portal:** browser console logs the build commit, or type `__BUILD__`.
  (Only self-reports if the deploy substitutes the build token — see §4.)

---

## 2. Rotating the live admin password

> **Status:** Done on 2026-06-16 (admin rotated, `JWT_SECRET` rotated). The
> procedure below is retained for the next time it is needed (new admin, suspected
> exposure, periodic rotation).

**Why this is manual:** the seed-password logic only runs for accounts that do
not yet exist. It never rewrites an existing admin row — so rotating a live
admin's password must be done by hand against the database.

### Method used (local, reliable) — direct DB update with a matched hash
The app hashes passwords as `salt$hexhash` using PBKDF2-HMAC-SHA256, 200,000
iterations (see `app/services/auth_service.py`). To rotate without the Render
shell:

1. Generate a hash for the new password locally (no app import needed):
   ```
   python -c "import hashlib, secrets, sys; pw=sys.argv[1]; salt=secrets.token_hex(16); dk=hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200000); print(salt + '$' + dk.hex())" "YOUR_NEW_PASSWORD"
   ```
2. Write it to the admin row via the external DB URL (psycopg2):
   `UPDATE users SET password_hash = '<salt$hash>', must_change_password = FALSE WHERE email = 'admin@nexuspayservices.com'`
   Expect `Rows updated: 1`.
3. **Save the new password in a password manager before logging out**, then test
   login. Setting `must_change_password = FALSE` means a permanent password with
   no forced-change screen; set `TRUE` instead to force a change on next login.

### Method B — Render Shell (if preferred)
Render → backend service → Shell → Python shell → load the admin row, set
`password_hash = hash_password(new)` and `must_change_password`, commit. (Note:
the Render web shell may block paste; right-click paste or the local method
above is more reliable.)

### Rotating JWT_SECRET
1. Render → backend service → Environment → set `JWT_SECRET` to a new strong
   random value (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
2. Save → service redeploys. **All existing sessions/tokens are invalidated;**
   everyone logs in again. Confirm `/health` is ok after redeploy, then log back in.

> Never store the admin password or `JWT_SECRET` in the repo, a chat, a terminal
> log, or this file.

---

## 3. Deploy-day sequence (Phase 0 → Phase 1)

**Order matters.** Set env vars before deploying, or the JWT guard halts boot.

1. **Env vars on Render first:**
   - `JWT_SECRET` — set and not the placeholder (the startup guard refuses to boot otherwise in production).
   - `ADMIN_SEED_PASSWORD`, `DEMO_SEED_PASSWORD` — for fresh-DB seeding.
   - `LOGIN_RATE_PER_IP_PER_MIN` (default 10), `LOGIN_MAX_FAILS` (default 5), `LOGIN_LOCKOUT_MINUTES` (default 15) — optional tunables, safe defaults.
2. Review all `cool-volta` diffs. **Confirm all six production domains are in `ALLOWED_ORIGINS`** (see ROADMAP reference table). A missing domain = that frontend is CORS-blocked after deploy.
3. Merge `cool-volta` → `main`.
4. Backend auto-redeploys on Render; migration `004` applies automatically.
5. Verify backend: `GET /health` → ok, 4 providers true, correct `git.commit_short`.
6. Redeploy the portal frontend (manual — see §4). Required so the paired change-password form ships with its backend.
7. **Immediately** rotate live admin password + `JWT_SECRET` (§2).
8. Verify end-to-end: rotated-admin login → forced-change flow works → run one audit → 4 providers respond → per-provider result cards render.

---

## 4. Deploying the portal frontend (manual)

The portal (`nexuspayai.com`) is **not** git-connected; it deploys manually.

### Option A — Netlify CLI (from the repo root)
```
netlify deploy --prod --dir "frontend" --site <NEXUSPAYAI_SITE_ID> --no-build
```
`--no-build` skips the repo's unrelated Netlify plugins.

### Option B — Drag-and-drop
Netlify dashboard → the `nexuspayai` site → drag the `frontend` folder onto the
deploy zone.

### Build self-report (optional, recommended)
The portal can report its deployed commit, but only if the deploy substitutes
the build token. This requires git-connecting the site (publish dir `frontend`,
a `sed` substitution build command injecting `$COMMIT_REF`/`$BRANCH`). Until
then, the portal reports "unknown" — honest by design, never a stale SHA.

> Decision pending (ROADMAP): switch the portal to git-connected auto-deploy.
> Benefit: auto-deploy on merge + working build self-report. Do this
> deliberately, not under time pressure.

---

## 5. Email service (Resend) — transactional email

> **Status:** Live as of 2026-06-22. Used for email verification AND password
> reset (both tokenized email-link flows, confirmed working on external Gmail).

- **Provider:** Resend. **Sending domain:** `send.nexuspayai.com` (subdomain,
  verified — DKIM + SPF pass). This is separate from the human M365 mailbox
  `marc@nexuspayai.com`; they don't conflict.
- **Env vars (Render):** `RESEND_API_KEY` (secret), `RESEND_FROM`
  (default `NexusPay <noreply@send.nexuspayai.com>`, env-overridable).
- **Code:** `app/services/email.py` → `send_email(to, subject, html)`. Uses
  `httpx`, returns `True`/`False` (never raises into the request path), logs
  failures with status + body to `nexuspay.email`.
- **Tracking:** click/open tracking intentionally **off** — security links must
  go straight to the domain, untracked, for deliverability and to avoid exposing
  tokens to a redirect layer.
- **Tokens:** verification/reset tokens live in `email_verification_tokens`
  (only a SHA-256 hash is stored; raw token lives only in the emailed link).
  Single-use, 24h expiry. `purpose` column distinguishes `verify` vs `reset`.
- **Deliverability note:** confirmed delivering to inbox for both the sending
  domain and external Gmail. If signups stall, first check whether verification
  mail is hitting spam; adding the optional **DMARC** record (still unset in
  Resend) is the next deliverability lever.
- **Free tier** covers launch volume; watch the Resend dashboard as signups grow.

---

## 5b. Auth flows (how the endpoints fit together)

All auth is backend-backed (FastAPI) with a JWT in `localStorage` (`np_jwt`).
The portal also has legacy client-side auth code (`allUsers`, `_pendingCodes`,
`doVerifyEmail`, the 6-digit branch of `doResetPassword`) that is **dead/bypassed
but still present** — do not wire anything new to it.

- **Login** `POST /api/login` — rate-limited per IP, per-account lockout, rejects
  unverified users ("Email not verified").
- **Register** `POST /api/register` — checks the exclusion list, creates the user
  `verified=False`, emails a tokenized verification link.
- **Verify email** `GET /api/verify-email?token=...` — flips `verified=True`,
  single-use token.
- **Resend verification** `POST /api/resend-verification` — enumeration-safe.
- **Forgot password** `POST /api/forgot-password` — enumeration-safe, emails a
  tokenized reset link (`/?reset_token=...`).
- **Reset password** `POST /api/reset-password` — body `{token, new_password}`,
  single-use token, sets password + clears `must_change_password`.
- **Change password** `POST /api/change-password` — authenticated, checks current
  password.
- **Admin reset user** `POST /api/users/{id}/reset-password` — admin-only,
  generates a temp password server-side, returns it for the admin to relay.
  Separate feature; leave intact.

Verification + reset both use the `email_verification_tokens` table, distinguished
by the `purpose` column (`verify` / `reset`). Tokens are SHA-256 hashed in the DB;
the raw token lives only in the emailed link. Links use a **root-path query param**
(`/?token=`, `/?reset_token=`) because the portal is a static single-file site —
a `/verify` path would 404 on Netlify before the JS runs.

---

## 5c. Deploy gotchas (hard-won)

- **`py_compile` does not catch missing imports.** A referenced-but-not-imported
  name is a runtime `NameError` → the app fails to boot. After adding an endpoint
  that uses a schema/model, grep the import line. Then verify the deploy by
  checking `/health`'s `git.commit_short` actually flipped to the new commit.
- **Render keeps the last-good deploy if a new one fails to boot.** A failed
  health check means production stays on the previous commit (it does NOT go
  down). So "the app is up" + "`/health` shows the OLD commit" = the new deploy
  failed; check Render logs.
- **Large `index.html` edits:** edit by explicit line indices (Python
  `lines[start:end] = [...]`), never a greedy `.*?` regex — a greedy match once
  deleted a ~329-line block. Check `(Get-Content index.html).Count` after every
  edit. If corrupted: `git checkout origin/main -- frontend/index.html`.
- **Commit frontend changes.** The portal is manual Netlify deploy (not
  git-connected); deploying without committing causes git-vs-production drift.
- **Env var names are case-sensitive** (a stray `RESEND_API_kEY` silently broke
  email — empty value, no error to the user, nothing in Resend logs).

---

## 6. AI provider engine — operational notes

- Four providers run in parallel (Claude, GPT-4o, Gemini, Grok); whichever have
  a valid key participate. Per-provider failures are surfaced in `_errors`, not
  silently swallowed.
- Provider/model names come from env vars (`*_MODEL`) and `config.py` defaults —
  upgrade a model by changing the env var, no code edit.
- Gemini requires the **Generative Language API** enabled on the Google Cloud
  project tied to the key, plus a valid `GOOGLE_API_KEY`. A key from a project
  without that API enabled will fail even though it looks valid.
- Grok on **image** statements is skipped unless `GROK_VISION_MODEL` is set.
- `/health` reports per-provider key presence (`true`/`false`) — this checks the
  key exists, not that it works. Confirm a real call before assuming a provider
  is live.

---

## 7. Database migrations

- Migrations live in `alembic/versions/`, chain `001 → 005`.
- They auto-apply on deploy via `alembic upgrade head` in the start command,
  plus `create_all` in the app lifespan (which adds missing *tables* but not
  missing *columns* — column changes require the migration).
- If a deploy ever returns 500s on endpoints that touch new columns, suspect an
  unapplied migration: confirm the start command ran `alembic upgrade head`.

---

## 8. Known limitations / conscious tradeoffs

- **Login rate limiter is in-memory, per-process.** Counters reset on restart;
  with `--workers 2` (production), each worker counts separately, so effective
  limits are roughly doubled. Hard global limits need a shared store (Redis) —
  out of scope for now.
- **Per-account lockout** can be abused to temporarily lock out a known user
  (lockout-DoS). The per-IP throttle limits this; accepted as standard.
- **No automated tests / CI.** Significant for a payments product — see ROADMAP
  parallel track.
- **Worker counts differ by environment:** production `--workers 2`, staging
  `--workers 1`.

---

## 9. Credential hygiene (standing policy)

- Secrets live only in Render environment variables.
- Never commit credentials, API keys, or connection strings to the repo.
- Never paste them into chats, terminals that get screenshotted, or documents.
- Any credential that has touched a chat, log, or screenshot is considered
  exposed and must be rotated.
- `contracts/` and any secret files are git-ignored — keep it that way.
