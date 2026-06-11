"""
Dafine — Auth Routes
POST /auth/register     — daftar akun baru
POST /auth/login        — login, return JWT
GET  /auth/me           — info user saat ini (protected)
PUT  /auth/password     — ganti password (protected)
PUT  /auth/api-key      — simpan/update OpenRouter API key (protected)
GET  /auth/api-key/status — cek apakah API key sudah diset (protected)
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr

from db import get_supabase
from security import (
    create_token,
    decrypt_api_key,
    encrypt_api_key,
    hash_password,
    verify_password,
    verify_token,
)

router        = APIRouter(prefix="/auth", tags=["auth"])
_http_bearer  = HTTPBearer()


# ─── MODELS ───────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class ApiKeyRequest(BaseModel):
    api_key: str


# ─── DEPENDENCY — current user ────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_http_bearer),
) -> dict:
    """
    Dependency: verifikasi JWT dari header Authorization: Bearer <token>.
    Mengembalikan row user dari database.
    """
    try:
        user_id = verify_token(credentials.credentials)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    sb     = get_supabase()
    result = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()

    # Ubah kondisi pengecekan di sini juga
    if not result or not result.data:
        raise HTTPException(status_code=401, detail="User not found.")

    return result.data


# ══════════════════════════════════════════════════════════════════
# POST /auth/register
# ══════════════════════════════════════════════════════════════════
@router.post("/register", status_code=201)
async def register(body: RegisterRequest):
    sb = get_supabase()

    # Cek apakah email sudah terdaftar
    existing = sb.table("users").select("id").eq("email", body.email).maybe_single().execute()
    # Tambahkan pengecekan apakah 'existing' tidak None
    if existing and existing.data:
        raise HTTPException(status_code=409, detail="Email already registered.")

    # Validasi password minimal 8 karakter
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

    # Hash password dengan Argon2id
    pw_hash = hash_password(body.password)

    # Insert ke database
    result = sb.table("users").insert({
        "email":         body.email,
        "password_hash": pw_hash,
    }).execute()

    user    = result.data[0]
    token   = create_token(user["id"])

    return {
        "message":      "Account created successfully.",
        "token":        token,
        "has_api_key":  False,
        "user": {
            "id":    user["id"],
            "email": user["email"],
        },
    }


# ══════════════════════════════════════════════════════════════════
# POST /auth/login
# ══════════════════════════════════════════════════════════════════
@router.post("/login")
async def login(body: LoginRequest):
    sb = get_supabase()

    # Fetch user by email
    result = sb.table("users").select("*").eq("email", body.email).maybe_single().execute()
    # Ubah kondisi pengecekan jika result bernilai None atau data kosong
    if not result or not result.data:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user = result.data

    # Verify password
    if not verify_password(user["password_hash"], body.password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token       = create_token(user["id"])
    has_api_key = bool(user.get("encrypted_api_key"))

    return {
        "token":       token,
        "has_api_key": has_api_key,
        "user": {
            "id":    user["id"],
            "email": user["email"],
        },
    }


# ══════════════════════════════════════════════════════════════════
# GET /auth/me
# ══════════════════════════════════════════════════════════════════
@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    return {
        "id":          current_user["id"],
        "email":       current_user["email"],
        "has_api_key": bool(current_user.get("encrypted_api_key")),
        "created_at":  current_user["created_at"],
    }


# ══════════════════════════════════════════════════════════════════
# PUT /auth/password
# ══════════════════════════════════════════════════════════════════
@router.put("/password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
):
    # Verifikasi password lama
    if not verify_password(current_user["password_hash"], body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters.")

    if body.current_password == body.new_password:
        raise HTTPException(status_code=422, detail="New password must be different from current password.")

    new_hash = hash_password(body.new_password)
    sb       = get_supabase()
    sb.table("users").update({
        "password_hash": new_hash,
        "updated_at":    "now()",
    }).eq("id", current_user["id"]).execute()

    return {"message": "Password updated successfully."}


# ══════════════════════════════════════════════════════════════════
# PUT /auth/api-key
# ══════════════════════════════════════════════════════════════════
@router.put("/api-key")
async def save_api_key(
    body: ApiKeyRequest,
    current_user: dict = Depends(get_current_user),
):
    if not body.api_key.startswith("sk-or-"):
        raise HTTPException(status_code=422, detail="Invalid OpenRouter API key format.")

    encrypted = encrypt_api_key(body.api_key)
    sb        = get_supabase()
    sb.table("users").update({
        "encrypted_api_key": encrypted,
        "updated_at":        "now()",
    }).eq("id", current_user["id"]).execute()

    return {"message": "API key saved successfully."}


# ══════════════════════════════════════════════════════════════════
# GET /auth/api-key/status
# ══════════════════════════════════════════════════════════════════
@router.get("/api-key/status")
async def api_key_status(current_user: dict = Depends(get_current_user)):
    return {"has_api_key": bool(current_user.get("encrypted_api_key"))}


# ══════════════════════════════════════════════════════════════════
# Helper — gunakan di main.py untuk endpoint /clean
# ══════════════════════════════════════════════════════════════════
def get_user_api_key(user: dict) -> str:
    """Decrypt dan return OpenRouter API key milik user."""
    encrypted = user.get("encrypted_api_key")
    if not encrypted:
        raise HTTPException(
            status_code=403,
            detail="OpenRouter API key not set. Please add it in Account Settings."
        )
    try:
        return decrypt_api_key(encrypted)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt API key.")