"""
Auth service — JWT creation, password hashing, token verification.
"""

import hashlib
import hmac
import secrets
import time
import json
import base64
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

security = HTTPBearer(auto_error=False)


# ── Password Hashing (bcrypt-style PBKDF2) ──────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, dk_hex = stored_hash.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ── JWT (manual HS256 — no PyJWT dependency) ─────────────────
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_token(user_id: str, email: str, role: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload_data = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + settings.JWT_EXPIRE_HOURS * 3600,
    }
    payload = _b64url(json.dumps(payload_data).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(settings.JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def decode_token(token: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b, payload_b, sig_b = parts
        # Verify signature
        signing_input = f"{header_b}.{payload_b}".encode()
        expected_sig = hmac.new(settings.JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        # Decode payload
        payload = json.loads(_b64url_decode(payload_b))
        # Check expiry
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ── Dependency: get current user from JWT ────────────────────
async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return payload


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    if not credentials:
        return None
    return decode_token(credentials.credentials)


def require_role(*roles):
    """Dependency factory: require the user to have one of the given roles."""
    async def checker(user: dict = Depends(get_current_user)):
        if user.get("role") not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return checker
