"""
Dafine — Security helpers
- Password  : Argon2id (hash/verify)
- API Key   : AES-256-GCM (encrypt/decrypt)
- Session   : JWT HS256
"""

import base64
import os
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ─── Argon2id ─────────────────────────────────────────────────────
_ph = PasswordHasher()

def hash_password(password: str) -> str:
    return _ph.hash(password)

def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _ph.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, VerificationError):
        return False


# ─── AES-256-GCM ──────────────────────────────────────────────────
def _master_key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError("ENCRYPTION_KEY env var is not set.")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError("ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return key

def encrypt_api_key(api_key: str) -> str:
    """Encrypt OpenRouter API key → base64(nonce + ciphertext)."""
    aesgcm = AESGCM(_master_key())
    nonce  = os.urandom(12)                        # 96-bit nonce
    ct     = aesgcm.encrypt(nonce, api_key.encode(), None)
    return base64.b64encode(nonce + ct).decode()

def decrypt_api_key(encrypted: str) -> str:
    """Decrypt stored ciphertext → original API key string."""
    data   = base64.b64decode(encrypted)
    nonce  = data[:12]
    ct     = data[12:]
    aesgcm = AESGCM(_master_key())
    return aesgcm.decrypt(nonce, ct, None).decode()


# ─── JWT HS256 ────────────────────────────────────────────────────
_JWT_SECRET    = os.getenv("JWT_SECRET", "change-me-in-production")
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_H  = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

def create_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(hours=_JWT_EXPIRE_H),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)

def verify_token(token: str) -> int:
    """Decode JWT → user_id (int). Raises ValueError on failure."""
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        return int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise ValueError("Session expired. Please log in again.")
    except jwt.InvalidTokenError as e:
        raise ValueError(f"Invalid token: {e}")