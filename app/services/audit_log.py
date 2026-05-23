"""
Append-only audit log - SOC 2 chain-of-custody / evidence.

Design rule: this module only ever INSERTS. There is intentionally no
update or delete function. Every material event (upload, view, download,
analyze, dedup hit, revision flag, export) is recorded with actor, time,
file hash, and request metadata so the control can be evidenced over a
SOC 2 Type 2 observation window.
"""

from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record(
    db: AsyncSession,
    *,
    action: str,
    actor: Optional[dict] = None,
    entity_type: str = "",
    entity_id: Optional[str] = None,
    file_hash: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    detail: Optional[dict] = None,
    commit: bool = False,
) -> AuditLog:
    """
    Insert one append-only audit-log row.

    `actor` is the get_current_user dict ({sub, email, role}) or None for
    public/no-auth events. Pass commit=True only when the caller is not
    already inside a transaction it will commit itself.
    """
    actor = actor or {}
    entry = AuditLog(
        action=action,
        actor_id=actor.get("sub"),
        actor_email=actor.get("email", ""),
        actor_role=actor.get("role", ""),
        entity_type=entity_type,
        entity_id=entity_id,
        file_hash=file_hash,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:1000] if user_agent else None,
        detail=detail or {},
    )
    db.add(entry)
    if commit:
        await db.commit()
    return entry