# NexusPay Intelligence — Launch Roadmap

> **Source of truth for the build path.** This file lives in the repo so it
> travels with the code and is visible to any collaborator or Claude Code
> session. Update it as phases complete.

**Last updated:** 2026-06-22 (later session — password reset shipped)
**Current main commit:** `97287a9` (password reset deployed; backend `a687b50`)
**Production status at last update:** healthy — all 4 AI providers live, R2 configured, login working, Phase 0 security hardening DEPLOYED, email verification DEPLOYED, password reset DEPLOYED (all via Resend). **Phase 0 is now fully closed.** `/health` exposes the live git commit.

---

## The North Star

**Stop presenting numbers. Start presenting evidence.**

Every figure on every screen traces to its source — the exact statement page
and verbatim line it came from — cross-verified across 4 AI models, with a
one-click receipt. A merchant can click any fee, see the proof, and hand it to
their current processor.

This is the differentiator. Competitors present numbers and ask for trust.
NexusPay presents the evidence behind every number.

---

## Governing principle: secure first, then ship

The phases are **ordered**, and the order is the discipline. A forensic /
compliance product cannot launch on bypassable authentication. **Phase 0 must
complete before any customer-facing launch.** Do not skip ahead to the
differentiator (Phase 2) before the foundation is secure and deployed.

---

## >> WHERE WE ARE (as of 2026-06-22, later session)

**Phase 0 is COMPLETE and fully deployed.** All security work is done: the two
CRITICAL holes closed, both credentials rotated, email verification live, and
**password reset now live** (tokenized server-side reset via emailed link,
confirmed working end-to-end on external Gmail). There is **no remaining
pre-customer security debt.**

The full auth surface is now real and backend-backed:
- Login (rate-limited, lockout, verified-gated)
- Registration (exclusion-enforced, email verification required)
- Email verification (tokenized link)
- Password reset (tokenized link)
- Forced password change (admin-seeded accounts)
- Admin reset-user-password (separate feature, intact)

**Next up → Phase 2 — the Provenance Engine**, the differentiator, whose
per-provider foundation is already in `main`. Estimated 4–6 focused sessions
(see Phase 2 section for the breakdown).

**Cosmetic / cleanup pending (none blocking):**
- Stale red banner "Enter the 6-digit reset code shown above" flashes on the
  reset-link page (`doResetPassword` leftover). Harmless — reset works. Fix when convenient.
- Orphaned client-side verify/reset code still in `index.html` (`_pendingCodes`,
  `doVerifyEmail`, `doResetPassword`'s 6-digit branch, `_sendVerificationEmail`,
  `_sendResetEmail`, `_composeResetEmail`) — fully bypassed, dead, safe to remove.
- Delete stale merged `claude/*` branches; remove stray untracked files
  (`app/config (1).py`, `app/services/ai_providers (2).py`, `frontend/.netlify/`);
  reconcile `docs/OPERATIONS.md` with the standalone copy.
- **DMARC record** still unset in Resend — optional, but the next deliverability
  lever before a real marketing push.

## >> LESSONS LEARNED (this session — worth remembering)

1. **`py_compile` does NOT catch missing imports.** A name referenced but not
   imported is a *runtime* `NameError`, not a syntax error — the file compiles
   clean but the app fails to boot (or the endpoint 500s). When an endpoint
   references a schema/model from another module, **verify the import line
   explicitly**, not just that it compiles. (Cost us a failed deploy:
   `ResetPasswordRequest` was used in auth.py but not imported.)
2. **Render holds the last-good deploy when a new one fails to boot.** The failed
   `0f2710e` deploy did NOT take production down — Render kept `3b3484a` live
   because the new version failed its health check. Always confirm the `/health`
   commit SHA actually flipped to the new commit after a deploy.
3. **Large single-file edits: use a Python line-range method, never greedy regex.**
   A `[regex]::Replace` with `.*?` in Singleline mode on the 7000-line
   `index.html` matched between two occurrences of a marker and **deleted a
   ~329-line block** (including a live function). Recovery was
   `git checkout origin/main -- frontend/index.html`. Going forward: edit by
   explicit line indices in Python, and **check `(Get-Content file).Count` after
   every edit** to catch corruption immediately.
4. **Deployed-but-not-committed is a real drift trap.** The portal is manual
   Netlify deploy (not git-connected), so it's easy to deploy `index.html` and
   forget to commit it. Always commit frontend changes so git = production.
5. **The portal has a global click-interceptor** (`document.addEventListener
   ('click', ...)` that pattern-matches `onclick` attribute strings and calls the
   matching `do*` function, with `stopImmediatePropagation`). Repointing a
   button's `onclick` attribute is NOT enough — the cleanest fix is to make the
   old handler detect the new case and delegate (e.g. `doResetPassword` checks
   `window._pendingResetToken` and calls `_submitResetFromLink`).
6. **Env var names are case-sensitive.** `RESEND_API_kEY` (stray lowercase k) is
   not `RESEND_API_KEY` — the app silently got an empty value and `send_email`
   bailed before calling Resend, with no email and nothing in Resend's logs.
7. **Static-host SPA routing:** a link to `/verify` 404s on Netlify (no such
   file). Use a root-path query param (`/?token=` , `/?reset_token=`) so
   `index.html` always loads and the JS handler reads the param.

---

## Phase 0 — Secure the foundation  *(COMPLETE — deployed 2026-06-16)*

Merged to `main` (commit `0352373`) and deployed. Backend + portal frontend live.

### Deployed and verified
- [x] Authenticate `/api/change-password` + current-password check (identity from JWT, not request body)
- [x] Paired frontend change-password form (collects current/temporary password)
- [x] Remove default seed credentials; env-var seeding; force admin password rotation on fresh DBs
- [x] CORS: removed `*.netlify.app` wildcard; explicit six-domain allowlist; credentials never paired with wildcard
- [x] Login rate limiting (per-IP) + per-account lockout
- [x] `JWT_SECRET` fail-fast startup guard (refuse to boot on empty/placeholder in production)
- [x] Build self-report: git SHA in `/health` (confirmed live, `0352373`). Portal meta tag present but reports "unknown" until the site is git-connected (manual deploy doesn't substitute the token — by design).

### Manual / deploy-day — DONE
- [x] **Rotated the live admin password** on the production DB (new password saved in password manager). Set directly with `must_change_password=FALSE` (permanent password, no forced-change ceremony).
- [x] **Rotated `JWT_SECRET`** on Render (all sessions invalidated; logged back in successfully on the new secret).

### Email verification + exclusion enforcement — DONE (deployed 2026-06-22, commit `0a441fd`)
- [x] Exclusion list enforced on public `/api/register` (closes the self-registration bypass)
- [x] Registration creates users `verified=False`; tokenized verification link emailed via Resend
- [x] `/api/verify-email` activates the account (single-use, 24h-expiring, hash-stored token)
- [x] `/api/resend-verification` (enumeration-safe — uniform response)
- [x] Frontend `/verify?token=` landing handler (`_verifyEmailFromLink`)
- [x] Resend email infrastructure: `send.nexuspayai.com` verified, `RESEND_API_KEY` in Render, external Gmail delivery confirmed
- [x] Migration `005_email_verification_tokens` applied on deploy

### Password reset — DONE (deployed 2026-06-22, commits `97287a9` / backend `a687b50`)
- [x] `forgot_password` rewritten: enumeration-safe, generates a `purpose="reset"` token, emails a reset link via Resend (no more on-screen temp password)
- [x] `/api/reset-password` validates the token (single-use, expiring, hash-stored), sets the new password, clears `must_change_password`, marks token used
- [x] `ResetPasswordRequest` schema extended with `token` field
- [x] Frontend: reset-link landing handler (`_resetPasswordFromLink` / `_submitResetFromLink`), `/?reset_token=` root-path link, `doResetPassword` delegates to the backend flow when a reset token is present
- [x] Reuses the `email_verification_tokens` table with `purpose="reset"` — no new migration
- [x] Confirmed working end-to-end on external Gmail (request → email → link → set password → login; single-use re-click correctly rejected)

**Phase 0 scope is now fully closed — no remaining pre-customer security items.**

---

## Phase 1 — Deploy what is already built  *(COMPLETE — deployed 2026-06-16)*

The per-provider results engine, IC role gates, exclusion list, and quote
engine were already complete in code; they are now live.

- [x] Merge `cool-volta` → `main`
- [x] Backend redeployed on Render (migration `004_provider_results` applied)
- [x] Redeployed portal frontend (`nexuspayai.com`)
- [x] Verified backend build SHA via `/health` (`0352373`). Portal self-report reads "unknown" by design (manual deploy) — see OPERATIONS.md §4.

> Next time at the portal: run one audit and confirm the per-provider result
> cards render (frontend + backend both current, so they should). If cards are
> empty, check the `/api/audit/run` response for `provider_results`.

---

## Phase 2 — The Provenance Engine  *(the differentiator — large build)*

Built on top of the per-provider foundation already in `main`.

- [ ] Extraction schema v2: every figure carries **source page + verbatim quote**
- [ ] Persist per-provider readings instead of discarding them after consensus
- [ ] Provenance data shape per field: `value`, `class` (EXTRACTED / DERIVED / ESTIMATED), `sources`, `checks`, `confidence band`
- [ ] One-click **receipt UI** on the portal (click any figure → see its evidence)
- [ ] Quality-gated caching + engine versioning + audit-log supersession (self-healing cache: defective extractions never become the cached authority; stale results re-run on next touch)
- [ ] Entity-level dedup with tolerance band (treat as duplicate unless figures differ beyond threshold)
- [ ] Arithmetic reconciliation pass + prominent NEEDS-REVIEW gating

**Rough effort (working in focused sessions, one sub-component at a time):**
This is the largest build remaining — realistically **several sessions**, not one.
A sensible order that ships value incrementally:
1. *Schema v2 + persist per-provider readings + provenance data shape* — the
   backend foundation. The biggest single piece (prompt rework + extraction
   changes + DB persistence). ~1–2 sessions.
2. *Receipt UI on the portal* — the visible payoff (click a figure → evidence).
   ~1 session once the data shape exists.
3. *Arithmetic reconciliation + NEEDS-REVIEW gating* — ~half a session.
4. *Quality-gated caching + engine versioning (self-healing cache)* — ~1 session.
5. *Entity-level dedup with tolerance band* — ~half a session.

So **4–6 focused sessions** end to end, with something demonstrable after step 2.
Caveat: estimates assume the per-provider foundation behaves as expected; the
first real obstacle is usually getting the 4 models to *reliably* emit page +
verbatim quote per figure (prompt-engineering iteration), which can stretch
step 1. Build it on a branch, ship sub-components as they're verified.

**Note on accuracy claims:** do not market "100% accurate." The defensible
posture is "4-AI cross-verified with automatic discrepancy flagging." Estimated
fields carry a visible badge and a per-field confidence band (Bloomberg shows
estimates with an "E" flag — same principle). Core figures (volume, total fees,
transaction count) are never estimated; if they can't be extracted, the audit
goes to NEEDS REVIEW rather than fabricating the foundation.

---

## Phase 3 — The Dispute Pack  *(deal-closer — medium, rides on provenance)*

- [ ] Provenance receipts auto-generate a letter to the merchant's current processor, citing each flagged line with its exhibit (page + verbatim quote + benchmark)

This is the artifact competitors do not have — it turns an audit into something
a merchant can act on immediately.

---

## Phase 4 — One backend, all surfaces  *(medium)*

- [ ] Extend the provenance API to `paycalculator`, `freeanalysis`, and the dashboard
- [ ] Dashboard provenance use is **internal verification** (QC before a proposal goes out), not customer-facing wow

---

## Parallel track — should-fix before real customers

- [ ] Real password reset + email verification (covered partly in Phase 0)
- [ ] Smoke tests on auth + audit paths (there are currently **no tests / no CI** — the quiet risk for a payments product)
- [ ] `pip-audit` on dependencies; bump `python-multipart`; dedupe `pypdf`

---

## Deploy-day sequence (Phase 0 → Phase 1) — order matters

1. Set/confirm env vars on Render **first**: `JWT_SECRET` (set, not placeholder), the three `LOGIN_*` tunables, `ADMIN_SEED_PASSWORD`, `DEMO_SEED_PASSWORD`. (Deploy before this and the JWT guard halts boot.)
2. Review all `cool-volta` diffs clear-headed; confirm **all six production domains** are in `ALLOWED_ORIGINS`.
3. Merge `cool-volta` → `main`.
4. Verify backend boots healthy (`/health` returns ok + 4 providers).
5. Redeploy portal frontend (ships the paired change-password form).
6. **Immediately** run the live-admin rotation + `JWT_SECRET` rotation (OPERATIONS.md §2).
7. Verify: rotated-admin login, forced-change flow works, run one audit, confirm 4 providers + provider-result cards render.

---

## Reference — production surfaces

| Surface | URL | Notes |
|---|---|---|
| Portal (Command Center) | https://nexuspayai.com | The provenance UI lives here; manual deploy |
| Pricing calculator | https://paycalculator.nexuspayai.com | Separate repo/site |
| Free analysis (landing) | https://freeanalysis.nexuspayservices.com | Lead-capture |
| Dashboard (internal) | https://dashboard.nexuspayservices.com | Admin/internal; separate repo |
| Matrix (admin-only) | https://matrix.nexuspayai.com | Admin-only |
| Main website | https://nexuspayservices.com | Marketing |

Backend (all surfaces): Render — unified FastAPI service.

> All six origins must be present in the CORS `ALLOWED_ORIGINS` allowlist, or
> the omitted surface will be blocked after the CORS fix deploys.
