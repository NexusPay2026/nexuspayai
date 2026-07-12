# CLAUDE.md — NexusPay Intelligence Platform

> This file is read automatically by Claude Code at the start of every session.
> It is the persistent operating context for this repository: architecture,
> conventions, hard-won lessons, standing rules, and current state. Keep it
> updated as the system evolves.

---

## 0. How to work in this repo (read first)

- **One step at a time on anything that touches infrastructure or deploys.**
  Propose the plan, make one change, verify it, then proceed. Do not batch
  multiple risky edits.
- **Verify, don't assume.** "It compiled" is NOT "it works." After any backend
  change that deploys, confirm `/health` shows the NEW commit SHA (see §5).
  After any endpoint change, exercise the endpoint (a junk-input call that
  returns a clean 4xx proves the route loaded and its DB query runs).
- **Scope before building.** Read the relevant existing code first and propose
  an approach before writing. This is a payments product; surprises are
  expensive.
- **Communication:** bottom-line-up-front, precise, no fluff. Flag risks,
  tradeoffs, and dependencies explicitly. Conservative/defensible over
  optimistic.
- **Environment:** Windows + PowerShell. Statement separator is `;` not `&&`.
  Non-standard Downloads folder: `C:\Users\marca\Documents\Downloads`.

---

## 1. Architecture (what runs where)

**Backend (production)**
- Host: Render, service `nexuspay-api-ochi` → `https://nexuspay-api-ochi.onrender.com`
- Git-connected to the `main` branch of GitHub `NexusPay2026/nexuspayai`.
  **Pushing to `main` auto-deploys.** Runs `alembic upgrade head` on boot.
  Runs with `--workers 2`.
- Stack: FastAPI, PostgreSQL 18, SQLAlchemy (async, asyncpg), Pydantic,
  Uvicorn, Alembic. Cloudflare R2 for object storage.

**Frontend — portal (production)**
- `nexuspayai.com` = Netlify, **MANUAL deploy, NOT git-connected.** Deploy with:
  ```
  netlify deploy --prod --dir "frontend" --site 849106fb-83b8-4dc6-9ed1-b910054a5ee7 --no-build
  ```
- Single-file `frontend/index.html` (~7000+ lines), React 18 via Babel-standalone,
  jsPDF. Because it's a static single-file site, **client-side routing must use
  root-path query params** (`/?token=`), never a path like `/verify` (which
  404s on Netlify before the JS runs).
- Because the portal is manual-deploy, **always commit `frontend/index.html`
  after deploying** so git matches production (see Lesson #4).

**Other surfaces**
- `paycalculator.nexuspayai.com` — public pricing calculator
- `freeanalysis.nexuspayservices.com` — free statement analysis landing page
- Business model: lead capture via free statement uploads → AI audit → merchant
  conversion (NOT SaaS subscription fees).

**Health check (the deploy-verification tool)**
- `GET /health` returns: `status`, `version`, a `git` block
  (`commit`, `commit_short`, `branch`), `r2_configured`, and `ai_providers`
  (4 booleans). **`commit_short` is how you confirm what's actually live.**

**Config**
- `app/config.py` is a `@dataclass Settings` reading `os.getenv(...)` with
  `__post_init__` boot guards (fails fast on missing `DATABASE_URL` or
  placeholder `JWT_SECRET`). Model names and tuning are env-overridable.
- Everything secret is read server-side from Render env vars. Rotating a key =
  update the env var in Render; the service restarts and picks it up.

**Auth internals**
- `app/services/auth_service.py`: `hash_password` = PBKDF2-HMAC-SHA256, 200k
  iterations, stored as `salt$hexhash`. JWT is manual HS256 (no PyJWT);
  identity is in `token_data["sub"]`.

**AI engine**
- Four providers run in parallel: Anthropic (Claude), OpenAI (GPT-4o),
  Google (Gemini), xAI (Grok). Whichever have a key configured participate.
  `temperature=0` for deterministic arithmetic. Per-provider failures are
  captured and returned (`_errors`), never silently swallowed.
- **KNOWN STRUCTURAL ISSUE:** there are three duplicated AI-provider
  implementations (`ai_providers.py`, `pricing_ai.py`, `pricing_tool.py`) that
  are NOT unified. A change to one engine does not propagate to the others.
  Unifying these is worth doing before/within Phase 2.

---

## 2. Auth flows (all backend-backed; JWT in localStorage `np_jwt`)

The portal ALSO contains legacy client-side auth code (`allUsers`,
`_pendingCodes`, `doVerifyEmail`, the 6-digit branch of `doResetPassword`,
`_sendVerificationEmail`, `_sendResetEmail`, `_composeResetEmail`). This is
**dead/bypassed but still present.** Do NOT wire anything new to it; it is
slated for removal.

- **Login** `POST /api/login` — rate-limited per IP, per-account lockout,
  rejects unverified users.
- **Register** `POST /api/register` — checks exclusion list, creates user
  `verified=False`, emails a tokenized verification link.
- **Verify email** `GET /api/verify-email?token=...` — flips `verified=True`,
  single-use.
- **Resend verification** `POST /api/resend-verification` — enumeration-safe.
- **Forgot password** `POST /api/forgot-password` — enumeration-safe, emails a
  tokenized reset link (`/?reset_token=...`).
- **Reset password** `POST /api/reset-password` — body `{token, new_password}`,
  single-use.
- **Change password** `POST /api/change-password` — authenticated, checks
  current password.
- **Admin reset user** `POST /api/users/{id}/reset-password` — admin-only,
  generates a temp password server-side and returns it for the admin to relay.
  Separate feature; leave intact.

Verification + reset share the `email_verification_tokens` table, distinguished
by the `purpose` column (`verify` / `reset`). Tokens are SHA-256 hashed in the
DB; the raw token lives only in the emailed link. Migrations chain `001 → 005`.

---

## 3. Email (Resend) — transactional only

- Provider: Resend. Sending domain: `send.nexuspayai.com` (subdomain, DKIM+SPF
  verified). Separate from the human M365 mailbox `marc@nexuspayai.com`.
- Env vars: `RESEND_API_KEY`, `RESEND_FROM`
  (`NexusPay <noreply@send.nexuspayai.com>`).
- Code: `app/services/email.py` → `send_email(to, subject, html)`. Uses httpx,
  returns True/False (never raises into the request path), logs to
  `nexuspay.email`.
- Click/open tracking intentionally OFF (security links go straight, untracked).
- DMARC record still UNSET in Resend — optional; the next deliverability lever
  before a real marketing push.

---

## 4. Standing rules (non-negotiable)

- **Legal entity name is `Nexus Pay, LLC`** on all formal docs/contracts. NOT
  "NexusPay Services, LLC." (`nexuspayservices.com` is an email/domain only.)
  Business address: 11150 E Mississippi Ave, Ste 309, Aurora, CO 80012.
- **NEVER store credentials** (passwords, API keys, `DATABASE_URL`, connection
  strings) in code, in this file, or in any committed artifact. Secrets live
  only in Render env vars and the password manager.
- **Personnel:** Hunter Sohl, Alexis Riney, and Matt deLisle have **"no formal
  relationship"** with Nexus Pay, LLC — never "employee," "fired," or
  "terminated" in any document. Hunter and Matt are on the live exclusion list
  and blocked from self-registration.
- **Git workflow:** prefer feature branch → PR → merge → auto-deploy. Direct
  push to `main` only when multi-account GitHub complexity makes PRs
  impractical. Always commit frontend changes (portal is manual-deploy).
- **Roadmap hygiene:** every session must update `docs/ROADMAP.md` checkboxes
  and the "Last updated" line before its final commit.
- **CI is green or it doesn't merge:** all future PRs must keep CI green
  (`.github/workflows/ci.yml` — pytest on sqlite + Postgres, plus pip-audit).
  New endpoints require at least one smoke test in `tests/` (see `tests/README.md`).
  Run `pytest` locally before pushing; never modify application logic just to make
  a test pass — if a test surfaces a real bug, fix the bug or report it in the PR.

---

## 5. Deploy sequence & verification

1. Make the change; for backend, confirm `python -m py_compile <files>` passes
   AND that any referenced schema/model is actually imported (see Lesson #1).
2. Commit only the intended files explicitly (the repo has known stray
   untracked files — `app/config (1).py`, `app/services/ai_providers (2).py`,
   `frontend/.netlify/` — that must NOT be added).
3. Push to `main` → Render auto-deploys (runs migrations on boot).
4. **Wait ~3-4 min, then confirm the deploy with `/health`:** the
   `git.commit_short` must equal your new commit. If it still shows the OLD
   commit, the new version FAILED to boot (see Lesson #2) — check Render logs.
5. Exercise any new/changed endpoint (junk-input → expect a clean 4xx, not 500).
6. For portal changes: after backend is confirmed live, run the manual
   `netlify deploy` (§1), then test in a fresh Incognito window.

---

## 6. LESSONS LEARNED (hard-won — do not relearn these)

1. **`py_compile` does NOT catch missing imports.** A referenced-but-unimported
   name is a runtime `NameError` → the app fails to boot. After adding an
   endpoint that uses a schema/model from another module, grep the import line
   explicitly. (Cost a failed deploy: `ResetPasswordRequest` was used in
   `auth.py` but not imported.)
2. **Render keeps the last-good deploy when a new one fails to boot.** A failed
   health check means production stays on the previous commit (it does NOT go
   down). So "app is up" + "`/health` shows the OLD commit" = the new deploy
   failed. Always confirm the SHA flipped.
3. **Large single-file edits: edit by explicit line indices (Python
   `lines[start:end] = [...]`), NEVER a greedy `.*?` regex.** A greedy
   Singleline regex on `index.html` once matched between two marker occurrences
   and deleted a ~329-line block (including a live function). Always check
   `(Get-Content file).Count` (or equivalent) after each edit. If corrupted:
   `git checkout origin/main -- frontend/index.html`. (In Claude Code, prefer
   the proper file-edit tools and review the diff.)
4. **Deployed-but-not-committed is a real drift trap.** The portal is manual
   Netlify deploy (not git-connected); it's easy to deploy `index.html` and
   forget to commit. Always commit so git = production.
5. **The portal has a global click-interceptor** — a
   `document.addEventListener('click', ...)` that pattern-matches buttons'
   `onclick` attribute strings, calls the matching `do*` function, and uses
   `stopImmediatePropagation`. Repointing a button's `onclick` is NOT enough;
   make the existing handler detect the new case and delegate (e.g.
   `doResetPassword` checks `window._pendingResetToken` → `_submitResetFromLink`).
6. **Env var names are case-sensitive.** `RESEND_API_kEY` (stray lowercase k)
   silently gave the app an empty value; `send_email` bailed before calling
   Resend, with no email sent and nothing in Resend's logs. The symptom was
   "the flow completes but no email arrives."
7. **Static-host SPA routing:** a link to `/verify` 404s on Netlify (no such
   file) before the JS runs. Use a root-path query param (`/?token=`,
   `/?reset_token=`) so `index.html` always loads and the handler reads the
   param.

---

## 7. Current state & roadmap

**Phase 0 (security) — COMPLETE and deployed.** Login hardening, CORS allowlist,
rate limiting/lockout, JWT fail-fast guard, env-var seeding, exclusion-enforced
registration, email verification, and password reset are all live and verified
end-to-end (including external Gmail). No remaining pre-customer security debt.

**Phase 1 (deploy what was built) — COMPLETE.** Per-provider results engine, IC
role gates, exclusion list, quote engine — all live.

**NEXT → Phase 2 — the Provenance Engine (the differentiator).** Every figure
traces to its source page + verbatim quote, cross-verified across the 4 models,
with a one-click receipt. Estimated 4–6 focused sessions. Suggested order:
1. Extraction schema v2 + persist per-provider readings + provenance data shape
   (`value`, `class` = EXTRACTED/DERIVED/ESTIMATED, `sources`, `checks`,
   `confidence band`). Biggest piece; the hard part is getting all 4 models to
   reliably emit page + verbatim quote per figure.
2. One-click receipt UI on the portal (the visible payoff).
3. Arithmetic reconciliation + prominent NEEDS-REVIEW gating.
4. Quality-gated caching + engine versioning (self-healing cache).
5. Entity-level dedup with tolerance band.

**Accuracy posture:** do NOT market "100% accurate." Use "4-AI cross-verified
with automatic discrepancy flagging." Estimated fields carry a visible badge +
confidence band. Core figures (volume, total fees, txn count) are never
estimated — if they can't be extracted, the audit goes to NEEDS REVIEW.

Later: Phase 3 (Dispute Pack — auto-generate a processor letter from provenance
receipts), Phase 4 (provenance across all surfaces).

**Cleanup backlog (low-risk, none blocking):**
- Cosmetic: stale red banner "Enter the 6-digit reset code shown above" flashes
  on the reset-link page (`doResetPassword` leftover). Reset works; just cosmetic.
- Remove the orphaned client-side verify/reset code (§2).
- Unify the three AI-provider implementations (§1).
- Delete stale merged `claude/*` branches; remove stray untracked files (§5);
  reconcile `docs/OPERATIONS.md` with the standalone copy.
- Add the DMARC record (§3).
- No tests / no CI anywhere — a quiet risk for a payments product. Worth adding
  smoke tests on the auth and audit paths, ideally before/within Phase 2.

---

## 8. Reference docs in this repo

- `docs/ROADMAP.md` — full phase plan and status (source of truth for the path).
- `docs/OPERATIONS.md` — runbook: architecture, admin password rotation, deploy
  sequence, email service, auth flows, deploy gotchas, migrations, credential
  hygiene.
