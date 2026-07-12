"""
Audit-log path smoke tests.

The append-only audit log (app.services.audit_log + AuditLog model) is the
chain-of-custody control. These tests prove:
  1. the record() service persists actor + action + timestamp, and
  2. a real read endpoint (GET /api/statements, admin-only) emits an audit row.

NOTE ON SCOPE (reported in the PR): the task asked for a test that "a merchant
create/update writes an audit-log row." In the current code, POST/PUT
/api/merchants do NOT call audit_log.record — merchant CRUD is not captured in
the audit log (only AI-audit runs in audit.py and statement reads in
statements.py are). Per the task's constraint, application logic was NOT changed
to make a test pass; instead the gap is documented by the xfail below and flagged
in the PR for a follow-up.
"""

from datetime import datetime

import pytest
from sqlalchemy import select

from conftest import make_user, token_for
from app.services import audit_log
from app.models import AuditLog


async def test_record_persists_actor_action_timestamp(db):
    actor = {"sub": "user-123", "email": "actor@example.com", "role": "admin"}
    await audit_log.record(
        db,
        action="merchant.update",
        actor=actor,
        entity_type="merchant",
        entity_id="merch-abc",
        detail={"field": "total_fees", "from": 100, "to": 120},
        commit=True,
    )

    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "merchant.update"
    assert row.actor_id == "user-123"
    assert row.actor_email == "actor@example.com"
    assert row.actor_role == "admin"
    assert row.entity_type == "merchant"
    assert row.entity_id == "merch-abc"
    # Timestamp is populated (tz-awareness of the read-back value is
    # backend-dependent — sqlite returns naive — so we only assert presence).
    assert isinstance(row.ts, datetime)


async def test_statements_view_writes_audit_row(client, db):
    """An admin read of /api/statements records a 'view' audit row carrying the
    actor identity, action, and timestamp."""
    admin = await make_user(db, email="auditadmin@example.com", role="admin")

    r = await client.get("/api/statements", headers={"Authorization": f"Bearer {token_for(admin)}"})
    assert r.status_code == 200, r.text

    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "view"
    assert row.entity_type == "statement_list"
    assert row.actor_email == "auditadmin@example.com"
    assert row.actor_role == "admin"
    assert row.ts is not None


async def test_statements_view_forbidden_for_non_admin_writes_no_row(client, db):
    """A non-admin is rejected before any audit row is written."""
    employee = await make_user(db, email="auditemp@example.com", role="employee")
    r = await client.get("/api/statements", headers={"Authorization": f"Bearer {token_for(employee)}"})
    assert r.status_code == 403
    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert rows == []


@pytest.mark.xfail(
    reason="GAP: POST/PUT /api/merchants do not call audit_log.record — merchant "
    "CRUD is not yet captured in the append-only log. Reported in PR; app logic "
    "intentionally left unchanged. Remove this xfail when merchant CRUD is wired "
    "to the audit log.",
    strict=False,
)
async def test_merchant_create_writes_audit_row(client, db):
    user = await make_user(db, email="merchowner@example.com", role="employee")
    r = await client.post(
        "/api/merchants",
        headers={"Authorization": f"Bearer {token_for(user)}"},
        json={"name": "Joe's Diner", "processor": "Square", "monthly_volume": 50000, "total_fees": 1500},
    )
    assert r.status_code == 201, r.text

    rows = (await db.execute(select(AuditLog).where(AuditLog.entity_type == "merchant"))).scalars().all()
    assert len(rows) >= 1, "expected an audit-log row for merchant creation"
    assert rows[0].actor_email == "merchowner@example.com"
    assert rows[0].action  # some create/update action
    assert rows[0].ts is not None
