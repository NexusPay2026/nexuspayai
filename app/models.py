"""
SQLAlchemy ORM models — all tables for the unified backend.
Postgres stores metadata. R2 stores files/objects.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime,
    JSON, ForeignKey, Index
)
from sqlalchemy.orm import relationship
from app.database import Base


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════
#  USERS
# ═══════════════════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=_uuid)
    email = Column(String(320), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(200), nullable=False)
    company = Column(String(300), default="")
    role = Column(String(20), default="user")  # admin | employee | client | user | demo
    tier = Column(String(20), default="free")   # free | pro | enterprise
    veteran = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    verified = Column(Boolean, default=False)
    must_change_password = Column(Boolean, default=False)
    assigned_merchants = Column(JSON, default=list)  # list of merchant IDs for clients
    created_by = Column(String(320), default="self")
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    last_login = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    merchants = relationship("Merchant", back_populates="owner", lazy="selectin")


# ═══════════════════════════════════════════════════════════════
#  MERCHANTS
# ═══════════════════════════════════════════════════════════════
class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(String, primary_key=True, default=_uuid)
    owner_id = Column(String, ForeignKey("users.id"), nullable=True)
    owner_email = Column(String(320), default="")
    name = Column(String(300), nullable=False)
    processor = Column(String(200), default="")
    statement_month = Column(String(20), default="")
    monthly_volume = Column(Float, default=0)
    total_fees = Column(Float, default=0)
    interchange_cost = Column(Float, default=0)
    processor_markup = Column(Float, default=0)
    monthly_fees = Column(Float, default=0)
    transaction_count = Column(Integer, default=0)
    credit_card_pct = Column(Float, default=85)
    avg_ticket = Column(Float, default=0)
    effective_rate = Column(Float, default=0)
    interchange_rate = Column(Float, default=0)
    markup_rate = Column(Float, default=0)
    risk_score = Column(Integer, default=0)
    line_items = Column(JSON, default=list)
    findings = Column(JSON, default=list)
    is_demo = Column(Boolean, default=False)
    added_by = Column(String(320), default="")
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Relationships
    owner = relationship("User", back_populates="merchants")

    # Stored file references (R2 keys)
    statement_r2_key = Column(String(500), nullable=True)
    report_r2_key = Column(String(500), nullable=True)


# ═══════════════════════════════════════════════════════════════
#  VISITORS / LEADS  (from landing page + website contact form)
# ═══════════════════════════════════════════════════════════════
class Visitor(Base):
    __tablename__ = "visitors"

    id = Column(String, primary_key=True, default=_uuid)
    full_name = Column(String(200), nullable=False)
    business_name = Column(String(300), nullable=False)
    email = Column(String(320), nullable=False, index=True)
    phone = Column(String(30), nullable=True)
    source = Column(String(50), default="landing_page")  # landing_page | website | referral
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    referrer = Column(Text, nullable=True)
    utm_source = Column(String(200), nullable=True)
    utm_medium = Column(String(200), nullable=True)
    utm_campaign = Column(String(200), nullable=True)
    utm_term = Column(String(200), nullable=True)
    utm_content = Column(String(200), nullable=True)
    page_url = Column(Text, nullable=True)
    session_duration_ms = Column(Integer, nullable=True)
    ai_business_type = Column(String(200), nullable=True)
    message = Column(Text, nullable=True)  # for contact form
    created_at = Column(DateTime(timezone=True), default=_now)


# ═══════════════════════════════════════════════════════════════
#  AUDIT JOBS  (server-side AI orchestration tracking)
# ═══════════════════════════════════════════════════════════════
class AuditJob(Base):
    __tablename__ = "audit_jobs"

    id = Column(String, primary_key=True, default=_uuid)
    merchant_id = Column(String, ForeignKey("merchants.id"), nullable=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    status = Column(String(30), default="pending")  # pending | processing | complete | failed
    provider_results = Column(JSON, default=dict)  # {claude: {...}, gpt: {...}, gemini: {...}}
    consensus_data = Column(JSON, default=dict)
    confidence = Column(String(20), nullable=True)
    error_message = Column(Text, nullable=True)
    statement_r2_key = Column(String(500), nullable=True)
    report_r2_key = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
    completed_at = Column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════════════════════════
#  INDEXES
# ═══════════════════════════════════════════════════════════════
Index("ix_merchants_owner", Merchant.owner_email)
Index("ix_visitors_created", Visitor.created_at.desc())
Index("ix_audit_jobs_user", AuditJob.user_id)


# ================================================================
#  PRICING QUOTES
# ================================================================
class Quote(Base):
    __tablename__ = "quotes"

    id = Column(Integer, primary_key=True, index=True)
    created_by = Column(String, nullable=False)
    created_by_role = Column(String, default="employee")
    created_at = Column(DateTime(timezone=True), default=_now)

    # Merchant profile
    merchant_name = Column(String, default="")
    vertical = Column(String, default="Other")
    risk_level = Column(String, default="low")
    volume = Column(Float, default=0)
    transactions = Column(Integer, default=0)

    # Sell pricing
    markup_pct = Column(Float, default=0)
    auth_sell = Column(Float, default=0)
    avs_sell = Column(Float, default=0)
    batch_sell = Column(Float, default=0)
    monthly_sell = Column(Float, default=0)
    transarmor_sell = Column(Float, default=0)
    pci_sell = Column(Float, default=0)
    has_amex = Column(Boolean, default=False)
    amex_volume = Column(Float, default=0)
    use_gateway = Column(Boolean, default=False)

    # Calculated results — Beacon
    beacon_trad_residual = Column(Float, default=0)
    beacon_trad_margin = Column(Float, default=0)
    beacon_flex_residual = Column(Float, default=0)
    beacon_flex_margin = Column(Float, default=0)

    # Calculated results — North  (added Apr 2026, backward-compatible default=0)
    north_residual = Column(Float, default=0)
    north_margin = Column(Float, default=0)

    # Calculated results — Kurv / EMS  (added Apr 2026, backward-compatible default=0)
    kurv_residual = Column(Float, default=0)
    kurv_margin = Column(Float, default=0)

    # Calculated results — Maverick
    maverick_residual = Column(Float, default=0)
    maverick_tnr = Column(Float, default=0)
    maverick_risk = Column(String, default="low")

    # Best program (across all active processors)
    best_program = Column(String, default="")
    best_residual = Column(Float, default=0)

    # Meta
    notes = Column(Text, default="")
    status = Column(String, default="draft")
    pdf_url = Column(String, default="")
