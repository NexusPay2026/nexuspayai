"""
NexusPay Intelligence — Database Models
"""
from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, Text, JSON
from sqlalchemy.sql import func
from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    company = Column(String(255), default="")
    role = Column(String(50), default="client")  # admin, employee, client, user, demo
    tier = Column(String(50), default="free")     # free, pro, enterprise
    veteran = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    verified = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)
    assigned_merchants = Column(JSON, default=list)  # list of merchant names for clients

    created_by = Column(String(255), default="system")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_login = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(DateTime(timezone=True), nullable=True)


class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    processor = Column(String(255), default="")
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

    # Ownership & audit trail
    owner_email = Column(String(255), index=True)  # who uploaded this
    added_by = Column(String(255))
    is_demo = Column(Boolean, default=False)
    dup_note = Column(String(500), default="")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_edited_by = Column(String(255), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    action = Column(String(100), nullable=False)   # login, password_reset, user_created, etc.
    actor_email = Column(String(255), nullable=False)
    target_email = Column(String(255), nullable=True)
    detail = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
