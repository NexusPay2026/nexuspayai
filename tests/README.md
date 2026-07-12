# Backend test suite

First automated tests for the NexusPay backend. Fast smoke coverage of the auth
surface (the crown jewels) and the audit-log chain-of-custody path.

## Run locally

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Runs in a couple of seconds against an **in-memory aiosqlite** database — no
Postgres, no network. All external calls (Resend email, AI providers, R2) are
mocked or never reached.

## Database strategy

The ORM models are portable (String UUID PKs, JSON, Float/Bool/DateTime — no
Postgres-only types), so the default test DB is aiosqlite in-memory. CI runs the
**same suite** a second time against the Postgres service container by setting
`TEST_DATABASE_URL`, so Postgres-specific behavior is exercised too:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/nexuspay_test pytest
```

Two tests are **skipped on sqlite** and only run on Postgres: they exercise the
token-expiry comparison in `auth.py` (`row.expires_at < now(tz)`), which relies
on timezone-aware `DateTime` storage that only Postgres provides (sqlite reads
back naive datetimes). They are marked `skipif(IS_SQLITE)` and run in CI.

## Safety

* `conftest.py` overwrites `DATABASE_URL` with a throwaway sqlite placeholder
  before importing the app, so the real production database is never touched.
* `_assert_test_db` refuses loudly to run against any non-sqlite URL that does
  not look like a test database (or that looks like production).

## Known gap flagged by these tests

`test_merchant_create_writes_audit_row` is an **xfail**: `POST/PUT /api/merchants`
do not currently write to the append-only audit log (only AI-audit runs and
statement reads do). Application logic was intentionally not changed to make the
test pass; wire merchant CRUD to `audit_log.record` and remove the xfail.

## Adding tests

New endpoints require at least one smoke test (see CLAUDE.md). Use the fixtures
in `conftest.py`: `client` (ASGI httpx client), `db` (a session for
seeding/asserting), and the `make_user` / `make_exclusion` / `token_for` helpers.
