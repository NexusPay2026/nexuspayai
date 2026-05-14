"""
Pydantic schemas — request and response models for all endpoints.
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Any
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════
class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(..., min_length=8)
    company: str = ""
    veteran: bool = False

class ChangePasswordRequest(BaseModel):
    email: EmailStr
    new_password: str = Field(..., min_length=8)

class AuthResponse(BaseModel):
    token: str
    email: str
    role: str
    display_name: str
    company: str = ""
    assigned_merchants: List[str] = []
    tier: str = "free"
    veteran: bool = False
    must_change_password: bool = False

class MeResponse(BaseModel):
    email: str
    role: str
    display_name: str
    company: str = ""
    assigned_merchants: List[str] = []
    tier: str = "free"
    veteran: bool = False
    must_change_password: bool = False


# ═══════════════════════════════════════════════════════════════
#  MERCHANTS
# ═══════════════════════════════════════════════════════════════
class MerchantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
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
    line_items: List[Any] = []
    findings: List[Any] = []

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
    line_items: Optional[List[Any]] = None
    findings: Optional[List[Any]] = None

class MerchantResponse(BaseModel):
    id: str
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
    effective_rate: float = 0
    interchange_rate: float = 0
    markup_rate: float = 0
    risk_score: int = 0
    line_items: List[Any] = []
    findings: List[Any] = []
    owner_email: str = ""
    is_demo: bool = False
    added_by: str = ""
    created_at: Optional[datetime] = None


# ═══════════════════════════════════════════════════════════════
#  USERS (Admin management)
# ═══════════════════════════════════════════════════════════════
class UserCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(..., min_length=8)
    company: str = ""
    role: str = "client"
    tier: str = "free"
    veteran: bool = False
    assigned_merchants: List[str] = []

class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    tier: Optional[str] = None
    veteran: Optional[bool] = None
    active: Optional[bool] = None
    verified: Optional[bool] = None
    assigned_merchants: Optional[List[str]] = None

class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    company: str = ""
    role: str
    tier: str = "free"
    veteran: bool = False
    active: bool = True
    verified: bool = False
    must_change_password: bool = False
    assigned_merchants: List[str] = []
    created_by: str = ""
    created_at: Optional[datetime] = None
    last_login: Optional[datetime] = None

class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8)


# ═══════════════════════════════════════════════════════════════
#  VISITORS / LEADS
# ═══════════════════════════════════════════════════════════════
class VisitorPayload(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    business_name: str = Field(..., min_length=2, max_length=300)
    email: EmailStr
    phone: str = Field(default="", max_length=30)
    source: str = "landing_page"
    referrer: Optional[str] = None
    page_url: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_term: Optional[str] = None
    utm_content: Optional[str] = None
    session_duration_ms: Optional[int] = None
    ai_business_type: Optional[str] = None
    message: Optional[str] = None

class VisitorResponse(BaseModel):
    id: str
    status: str = "captured"
    message: str = "Visitor data stored successfully."

class ContactPayload(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    phone: str = ""
    business_name: str = ""
    message: str = ""


# ═══════════════════════════════════════════════════════════════
#  AUDIT
# ═══════════════════════════════════════════════════════════════
class AuditRequest(BaseModel):
    """Trigger a server-side AI audit. File must already be uploaded to R2."""
    merchant_name: str = ""
    processor: str = ""
    r2_key: Optional[str] = None  # if statement was uploaded to R2
    manual_volume: Optional[float] = None
    manual_fees: Optional[float] = None

class AuditStatusResponse(BaseModel):
    id: str
    status: str
    confidence: Optional[str] = None
    consensus_data: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ═══════════════════════════════════════════════════════════════
#  STORAGE
# ═══════════════════════════════════════════════════════════════
class UploadURLResponse(BaseModel):
    upload_url: str
    r2_key: str
    expires_in: int = 3600

class DownloadURLResponse(BaseModel):
    download_url: str
    expires_in: int = 3600


# ================================================================
#  PRICING QUOTES
# ================================================================
class QuoteCreate(BaseModel):
    merchant_name: str = ""
    vertical: str = "Other"
    risk_level: str = "low"
    volume: float = 0
    transactions: int = 0
    markup_pct: float = 0
    auth_sell: float = 0
    avs_sell: float = 0
    batch_sell: float = 0
    monthly_sell: float = 0
    transarmor_sell: float = 0
    pci_sell: float = 0
    has_amex: bool = False
    amex_volume: float = 0
    use_gateway: bool = False
    results: dict = {}
    notes: str = ""


class QuoteResponse(BaseModel):
    id: int
    created_by: str
    created_at: str
    merchant_name: str
    vertical: str
    risk_level: str
    volume: float
    transactions: int
    markup_pct: float
    auth_sell: float
    avs_sell: float
    batch_sell: float
    monthly_sell: float
    transarmor_sell: float
    pci_sell: float
    has_amex: bool
    amex_volume: float
    use_gateway: bool
    beacon_trad_residual: float
    beacon_trad_margin: float
    beacon_flex_residual: float
    beacon_flex_margin: float
    north_residual: float = 0
    north_margin: float = 0
    kurv_residual: float = 0
    kurv_margin: float = 0
    maverick_residual: float
    maverick_tnr: float
    maverick_risk: str
    best_program: str
    best_residual: float
    notes: str
    status: str
    pdf_url: str


class QuoteListResponse(BaseModel):
    quotes: list[QuoteResponse]
    total: int
