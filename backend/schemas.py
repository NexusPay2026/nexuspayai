"""
NexusPay Intelligence — API Schemas
"""
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime


# ── Auth ──────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str

class LoginResponse(BaseModel):
    token: str
    email: str
    display_name: str
    role: str
    tier: str
    company: str
    veteran: bool
    must_change_password: bool
    assigned_merchants: list = []

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    company: str = ""
    veteran: bool = False

class ChangePasswordRequest(BaseModel):
    email: str
    new_password: str

class ResetPasswordRequest(BaseModel):
    email: str


# ── User Management (Admin) ──────────────────────
class OnboardUserRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "client"
    tier: str = "free"
    company: str = ""
    veteran: bool = False
    assigned_merchants: list = []

class EditUserRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    tier: Optional[str] = None
    veteran: Optional[bool] = None
    active: Optional[bool] = None
    assigned_merchants: Optional[list] = None
    new_password: Optional[str] = None

class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    company: str
    role: str
    tier: str
    veteran: bool
    active: bool
    verified: bool
    must_change_password: bool
    assigned_merchants: list
    created_by: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Merchants ─────────────────────────────────────
class MerchantCreate(BaseModel):
    name: str
    processor: str = ""
    statement_month: str = ""
    monthly_volume: float = 0
    total_fees: float = 0
    interchange_cost: float = 0
    processor_markup: float = 0
    monthly_fees: float = 0
    transaction_count: int = 0
    credit_card_pct: float = 85
    avg_ticket: float = 0
    risk_score: int = 0
    line_items: list = []
    findings: list = []

class MerchantUpdate(BaseModel):
    name: Optional[str] = None
    processor: Optional[str] = None
    statement_month: Optional[str] = None
    monthly_volume: Optional[float] = None
    total_fees: Optional[float] = None
    interchange_cost: Optional[float] = None
    processor_markup: Optional[float] = None
    monthly_fees: Optional[float] = None
    transaction_count: Optional[int] = None
    credit_card_pct: Optional[float] = None
    avg_ticket: Optional[float] = None
    risk_score: Optional[int] = None

class MerchantResponse(BaseModel):
    id: int
    name: str
    processor: str
    statement_month: str
    monthly_volume: float
    total_fees: float
    effective_rate: float
    risk_score: int
    owner_email: str
    is_demo: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
