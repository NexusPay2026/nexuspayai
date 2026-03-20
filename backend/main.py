"""
NexusPay Intelligence — API Server
FastAPI backend with PostgreSQL persistence.
All auth, user management, and merchant CRUD.
"""
import os
import secrets
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc
from typing import Optional

from . import models, auth, schemas
from .database import engine, get_db, Base

# ── Create tables ──────────────────────────────────
Base.metadata.create_all(bind=engine)

app = FastAPI(title="NexusPay Intelligence API", version="4.0")

# ── CORS (allow frontend on any origin during dev) ─
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Serve frontend ─────────────────────────────────
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="frontend")


# ── Auth dependency ────────────────────────────────
def get_current_user(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    payload = auth.decode_token(token)
    if not payload or "email" not in payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(models.User).filter(models.User.email == payload["email"]).first()
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Account not found or deactivated")
    return user


def require_admin(user: models.User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_editor(user: models.User = Depends(get_current_user)):
    if user.role not in ("admin", "employee"):
        raise HTTPException(status_code=403, detail="Edit access required")
    return user


# ── Audit logging ──────────────────────────────────
def log_action(db: Session, action: str, actor: str, target: str = "", detail: str = ""):
    entry = models.AuditLog(action=action, actor_email=actor, target_email=target, detail=detail)
    db.add(entry)
    db.commit()


# ── Seed admin on startup ─────────────────────────
@app.on_event("startup")
def seed_admin():
    db = next(get_db())
    try:
        admin = db.query(models.User).filter(models.User.email == "admin@nexuspayservices.com").first()
        if not admin:
            admin = models.User(
                email="admin@nexuspayservices.com",
                password_hash=auth.hash_password("NexusPay2026!"),
                display_name="NexusPay Admin",
                company="NexusPay Services",
                role="admin",
                tier="enterprise",
                active=True,
                verified=True,
                must_change_password=False,
                created_by="system",
            )
            db.add(admin)
            db.commit()
            print("✓ Admin account seeded: admin@nexuspayservices.com / NexusPay2026!")

        # Seed demo account
        demo = db.query(models.User).filter(models.User.email == "demo@nexuspayservices.com").first()
        if not demo:
            demo = models.User(
                email="demo@nexuspayservices.com",
                password_hash=auth.hash_password("Demo2026!"),
                display_name="Demo User",
                company="Demo",
                role="demo",
                tier="enterprise",
                active=True,
                verified=True,
                must_change_password=False,
                created_by="system",
            )
            db.add(demo)
            db.commit()
            print("✓ Demo account seeded: demo@nexuspayservices.com / Demo2026!")
    finally:
        db.close()


# ═══════════════════════════════════════════════════
#  AUTH ENDPOINTS
# ═══════════════════════════════════════════════════

@app.post("/api/login", response_model=schemas.LoginResponse)
def login(req: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        sqlfunc.lower(models.User.email) == req.email.strip().lower()
    ).first()

    if not user:
        raise HTTPException(status_code=401, detail="No account found for this email.")

    if not user.active:
        raise HTTPException(status_code=401, detail="Account deactivated. Contact admin@nexuspayservices.com.")

    if not auth.verify_password(req.password, user.password_hash):
        if user.must_change_password:
            raise HTTPException(status_code=401, detail="Your password was recently reset. Use the temporary password sent by your admin.")
        raise HTTPException(status_code=401, detail="Incorrect password. Use 'Forgot your password?' to reset it.")

    # Update last login
    user.last_login = datetime.now(timezone.utc)
    db.commit()

    token = auth.create_access_token({"email": user.email, "role": user.role})
    log_action(db, "login", user.email, detail=f"Role: {user.role}, Tier: {user.tier}")

    return schemas.LoginResponse(
        token=token,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tier=user.tier,
        company=user.company or "",
        veteran=user.veteran,
        must_change_password=user.must_change_password,
        assigned_merchants=user.assigned_merchants or [],
    )


@app.post("/api/register")
def register(req: schemas.RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(
        sqlfunc.lower(models.User.email) == req.email.strip().lower()
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    user = models.User(
        email=req.email.strip().lower(),
        password_hash=auth.hash_password(req.password),
        display_name=req.name.strip(),
        company=req.company.strip(),
        role="user",
        tier="free",
        veteran=req.veteran,
        active=True,
        verified=True,  # Skip email verification for now — add later
        must_change_password=False,
        created_by="self-register",
    )
    db.add(user)
    db.commit()
    log_action(db, "register", user.email, detail=f"Self-registered, veteran={req.veteran}")

    return {"message": "Account created successfully. Please sign in."}


@app.post("/api/change-password")
def change_password(req: schemas.ChangePasswordRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        sqlfunc.lower(models.User.email) == req.email.strip().lower()
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    user.password_hash = auth.hash_password(req.new_password)
    user.must_change_password = False
    user.password_changed_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, "password_changed", user.email, detail="User changed own password")

    return {"message": "Password updated successfully."}


@app.post("/api/forgot-password")
def forgot_password(req: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    """Generate a temporary password and return it. In production, email this."""
    user = db.query(models.User).filter(
        sqlfunc.lower(models.User.email) == req.email.strip().lower()
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email.")
    if not user.active:
        raise HTTPException(status_code=400, detail="Account deactivated.")

    # Generate temp password
    temp_pass = "NexusPay" + secrets.token_urlsafe(6) + "!"
    user.password_hash = auth.hash_password(temp_pass)
    user.must_change_password = True
    db.commit()
    log_action(db, "forgot_password", user.email, detail="Temp password generated")

    # In production, send via email. For now, return it.
    return {"message": "Temporary password generated.", "temp_password": temp_pass}


# ═══════════════════════════════════════════════════
#  USER MANAGEMENT (Admin)
# ═══════════════════════════════════════════════════

@app.get("/api/users", response_model=list[schemas.UserResponse])
def list_users(admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(models.User).order_by(models.User.created_at.desc()).all()
    return users


@app.post("/api/users/onboard", response_model=schemas.UserResponse)
def onboard_user(req: schemas.OnboardUserRequest, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(
        sqlfunc.lower(models.User.email) == req.email.strip().lower()
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    user = models.User(
        email=req.email.strip().lower(),
        password_hash=auth.hash_password(req.password),
        display_name=req.name.strip(),
        company=req.company.strip(),
        role=req.role,
        tier=req.tier,
        veteran=req.veteran,
        assigned_merchants=req.assigned_merchants,
        active=True,
        verified=True,
        must_change_password=True,
        created_by=admin.email,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(db, "user_onboarded", admin.email, user.email, f"Role: {req.role}, Tier: {req.tier}")

    return user


@app.put("/api/users/{user_id}")
def edit_user(user_id: int, req: schemas.EditUserRequest, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if req.display_name is not None:
        user.display_name = req.display_name
    if req.email is not None:
        # Check uniqueness
        dup = db.query(models.User).filter(
            sqlfunc.lower(models.User.email) == req.email.lower(),
            models.User.id != user_id
        ).first()
        if dup:
            raise HTTPException(status_code=400, detail="Email already used by another account.")
        user.email = req.email.lower()
    if req.company is not None:
        user.company = req.company
    if req.role is not None:
        user.role = req.role
    if req.tier is not None:
        user.tier = req.tier
    if req.veteran is not None:
        user.veteran = req.veteran
    if req.active is not None:
        if user.email == "admin@nexuspayservices.com" and req.active is False:
            raise HTTPException(status_code=400, detail="Cannot deactivate primary admin.")
        user.active = req.active
    if req.assigned_merchants is not None:
        user.assigned_merchants = req.assigned_merchants
    if req.new_password:
        if len(req.new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be 8+ characters.")
        user.password_hash = auth.hash_password(req.new_password)
        user.must_change_password = True
        log_action(db, "password_reset_admin", admin.email, user.email, "Admin reset password")

    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, "user_edited", admin.email, user.email)

    return {"message": f"Updated: {user.display_name}"}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.email == "admin@nexuspayservices.com":
        raise HTTPException(status_code=400, detail="Cannot delete primary admin.")

    name = user.display_name
    db.delete(user)
    db.commit()
    log_action(db, "user_deleted", admin.email, user.email)

    return {"message": f"Deleted: {name}"}


@app.post("/api/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    temp_pass = "NexusPay" + secrets.token_urlsafe(6) + "!"
    user.password_hash = auth.hash_password(temp_pass)
    user.must_change_password = True
    user.password_changed_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, "password_reset_admin", admin.email, user.email, f"Temp: {temp_pass}")

    return {
        "message": f"Password reset for {user.display_name}",
        "temp_password": temp_pass,
        "email": user.email,
        "display_name": user.display_name,
    }


# ═══════════════════════════════════════════════════
#  MERCHANT ENDPOINTS
# ═══════════════════════════════════════════════════

@app.get("/api/merchants")
def list_merchants(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role in ("admin", "employee"):
        merchants = db.query(models.Merchant).order_by(models.Merchant.created_at.desc()).all()
    elif user.role == "demo":
        merchants = db.query(models.Merchant).filter(models.Merchant.is_demo == True).all()
    elif user.role == "client":
        assigned = [n.lower().strip() for n in (user.assigned_merchants or [])]
        merchants = db.query(models.Merchant).filter(
            (sqlfunc.lower(models.Merchant.owner_email) == user.email.lower()) |
            (sqlfunc.lower(models.Merchant.name).in_(assigned))
        ).all()
    else:
        merchants = db.query(models.Merchant).filter(
            sqlfunc.lower(models.Merchant.owner_email) == user.email.lower()
        ).all()
    return merchants


@app.post("/api/merchants")
def create_merchant(req: schemas.MerchantCreate, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Duplicate check
    vol = req.monthly_volume or 0
    eff_rate = (req.total_fees / vol * 100) if vol > 0 else 0

    existing = db.query(models.Merchant).filter(
        sqlfunc.lower(models.Merchant.name) == req.name.strip().lower(),
        models.Merchant.statement_month == req.statement_month,
    ).first()

    if existing:
        existing_rate = existing.effective_rate or 0
        if abs(existing_rate - eff_rate) < 0.05 and abs((existing.total_fees or 0) - req.total_fees) < 1.0:
            if user.role != "admin":
                raise HTTPException(status_code=400, detail=f"Duplicate: {req.name} ({req.statement_month}) already exists with identical data.")
            # Admin override — update existing
            for field in ["processor", "monthly_volume", "total_fees", "interchange_cost",
                          "processor_markup", "monthly_fees", "transaction_count",
                          "credit_card_pct", "avg_ticket", "risk_score", "line_items", "findings"]:
                setattr(existing, field, getattr(req, field))
            existing.effective_rate = eff_rate
            existing.last_edited_by = user.email
            db.commit()
            return {"message": "Duplicate updated (admin override)", "id": existing.id}

    merchant = models.Merchant(
        name=req.name.strip(),
        processor=req.processor,
        statement_month=req.statement_month,
        monthly_volume=req.monthly_volume,
        total_fees=req.total_fees,
        interchange_cost=req.interchange_cost,
        processor_markup=req.processor_markup,
        monthly_fees=req.monthly_fees,
        transaction_count=req.transaction_count,
        credit_card_pct=req.credit_card_pct,
        avg_ticket=req.avg_ticket,
        effective_rate=eff_rate,
        interchange_rate=(req.interchange_cost / vol * 100) if vol > 0 else 0,
        markup_rate=(req.processor_markup / vol * 100) if vol > 0 else 0,
        risk_score=req.risk_score,
        line_items=req.line_items,
        findings=req.findings,
        owner_email=user.email,
        added_by=user.email,
    )
    db.add(merchant)
    db.commit()
    db.refresh(merchant)
    return {"message": "Merchant added", "id": merchant.id}


@app.put("/api/merchants/{merchant_id}")
def update_merchant(merchant_id: int, req: schemas.MerchantUpdate, editor: models.User = Depends(require_editor), db: Session = Depends(get_db)):
    m = db.query(models.Merchant).filter(models.Merchant.id == merchant_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found.")

    for field, value in req.dict(exclude_none=True).items():
        setattr(m, field, value)

    # Recalculate derived fields
    vol = m.monthly_volume or 1
    m.effective_rate = (m.total_fees / vol * 100) if vol > 0 else 0
    m.interchange_rate = (m.interchange_cost / vol * 100) if vol > 0 else 0
    m.markup_rate = (m.processor_markup / vol * 100) if vol > 0 else 0
    m.last_edited_by = editor.email
    m.updated_at = datetime.now(timezone.utc)
    db.commit()

    return {"message": f"Updated: {m.name}"}


@app.delete("/api/merchants/{merchant_id}")
def delete_merchant(merchant_id: int, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    m = db.query(models.Merchant).filter(models.Merchant.id == merchant_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found.")
    name = m.name
    db.delete(m)
    db.commit()
    return {"message": f"Deleted: {name}"}


# ═══════════════════════════════════════════════════
#  HEALTH & INFO
# ═══════════════════════════════════════════════════

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "4.0", "platform": "NexusPay Intelligence"}


@app.get("/api/me")
def get_me(user: models.User = Depends(get_current_user)):
    return {
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "tier": user.tier,
        "company": user.company,
        "veteran": user.veteran,
        "must_change_password": user.must_change_password,
        "assigned_merchants": user.assigned_merchants or [],
    }


# ── Serve frontend index ──────────────────────────
@app.get("/")
def serve_frontend():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return {"message": "NexusPay Intelligence API v4.0 — frontend not found, use /docs for API"}


@app.get("/__init__.py")
def noop():
    return {}
