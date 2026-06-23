# Claude Code kickoff — Phase 2, Step 1 (Provenance Engine: extraction schema v2)

> Paste everything below the line into Claude Code, run from the repo root.
> CLAUDE.md is read automatically — this only adds the locked scope + first move.

---

We're starting **Phase 2, Step 1** from CLAUDE.md §7: extraction schema v2 +
persist per-provider readings + a provenance data shape. Follow §0 (one step at
a time, verify don't assume, scope before building) and §5 (deploy + verify).

## Decisions already locked (do not re-litigate)
- **Strictly additive.** Keep every existing flat field in the extraction JSON
  exactly as-is. Provenance is a NEW parallel block; existing parsing, the
  `Merchant` columns, and the frontend's reads of `data` / `provider_results`
  must keep working unchanged.
- **No migration this session.** Provenance lives inside the EXISTING JSON
  columns: consensus-level provenance rides in `consensus_data` (already
  persisted as the whole result dict on `AuditJob`); per-provider provenance
  rides in the existing `provider_results` JSON column. No schema change, no
  Alembic migration, no boot-migration risk.

## Provenance contract (the data shape)
Per figure, a provenance object:
```json
{
  "value": <number>,
  "class": "EXTRACTED" | "DERIVED" | "ESTIMATED",
  "page": <int|null>,
  "quote": "<verbatim text from the statement|null>",
  "basis": "<formula, only for DERIVED, e.g. total_fees/monthly_volume*100|null>",
  "confidence": <float 0-1>
}
```
Rules:
- `EXTRACTED` → MUST have `page` and a verbatim `quote`.
- `DERIVED` → MUST have `basis`; `page`/`quote` null.
- `ESTIMATED` → carries the badge + confidence band.
- **Core figures (`monthly_volume`, `total_fees`, `transaction_count`) may
  never be ESTIMATED.** If a model can't EXTRACT one, that figure's class is
  flagged so the audit can route to NEEDS REVIEW (§7 accuracy posture).

## Scope of Step 1 (backend only — receipt UI is Step 2)
1. **Prompt v2** in `app/services/ai_providers.py` (`AI_EXTRACTION_PROMPT`):
   append a `provenance` object to the schema asking each model for page +
   verbatim quote + class + confidence per figure. Do NOT remove or rename any
   existing field. This is the hard part — getting all 4 models to reliably
   emit page + quote.
2. **Retention**: widen `_provider_summary` (and `_build_provider_results` in
   `app/routers/audit.py`) so each provider's `provenance` survives instead of
   being stripped to 5 fields.
3. **Consensus**: in `_build_consensus`, build a reconciled
   `consensus["provenance"]` with cross-provider checks (do models cite the
   same page/quote for a figure?) + a confidence band. Keep the existing
   numeric consensus math untouched.
4. **Persist**: confirm `consensus["provenance"]` and the per-provider
   provenance land in `consensus_data` / `provider_results` (no new columns).

## First move (one change, then stop and verify)
Start with **Prompt v2 only**. Then:
- `python -m py_compile app/services/ai_providers.py`
- Grep the imports actually used (Lesson #1 — py_compile won't catch a missing
  import).
- Show me the diff before committing. Commit ONLY `app/services/ai_providers.py`
  (the repo has known stray untracked files — do NOT `git add -A`).
- Push to `main`, wait ~3-4 min, confirm `/health` `git.commit_short` flipped
  to the new SHA (a stale SHA = failed boot, §5/Lesson #2).
- Exercise `POST /api/audit/run` with junk input → expect a clean 4xx, and if a
  real statement is handy, confirm a live extraction now returns `provenance`.

Then propose Step 2 (retention) and stop for review before proceeding.

## Standing reminders pulled forward
- Windows/PowerShell: statement separator is `;` not `&&`.
- The portal is manual Netlify deploy — not in scope this step (backend only).
- Never commit secrets; keys stay in Render env vars.
