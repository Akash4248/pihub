"""
Mentora AI Tutor - Authentication & User Cloud Key Security Module
Provides JWT Token Generation, Verification, Password Hashing, and User Profile Data Storage.
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import Header, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "mentora_k12_jwt_secret_2026_change_in_prod_7781a")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_SECONDS = 60 * 60 * 24 * 30  # 30 Days

# DB Path for User Profiles & Auth
_default_db = os.path.join(os.path.dirname(__file__), "..", "..", ".local_run", "mentora_users.db")
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", _default_db)
if not os.path.isabs(AUTH_DB_PATH):
    AUTH_DB_PATH = os.path.abspath(AUTH_DB_PATH)

try:
    os.makedirs(os.path.dirname(AUTH_DB_PATH), exist_ok=True)
except (PermissionError, OSError):
    AUTH_DB_PATH = os.path.expanduser("~/.mentora/mentora_users.db")
    os.makedirs(os.path.dirname(AUTH_DB_PATH), exist_ok=True)

# Initialize Auth SQLite Database
def _init_auth_db():
    conn = sqlite3.connect(AUTH_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT DEFAULT 'student',  -- 'student', 'teacher', 'parent'
            grade INTEGER DEFAULT 9,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

_init_auth_db()

# Cryptographic Password Hashing (PBKDF2-HMAC-SHA256)
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return base64.b64encode(salt + pwd_hash).decode('utf-8')

def verify_password(password: str, stored_hash_str: str) -> bool:
    try:
        decoded = base64.b64decode(stored_hash_str.encode('utf-8'))
        salt = decoded[:16]
        stored_pwd_hash = decoded[16:]
        new_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return hmac.compare_digest(stored_pwd_hash, new_hash)
    except Exception:
        return False

# Base64URL Encoding/Decoding for Zero-Dependency JWT Implementation
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')

def _b64url_decode(data_str: str) -> bytes:
    padding = '=' * (4 - (len(data_str) % 4))
    return base64.urlsafe_b64decode((data_str + padding).encode('utf-8'))

def create_access_token(data: dict) -> str:
    header = {"alg": ALGORITHM, "typ": "JWT"}
    payload = data.copy()
    now = int(time.time())
    payload.update({
        "iat": now,
        "exp": now + ACCESS_TOKEN_EXPIRE_SECONDS
    })
    
    header_bytes = json.dumps(header, separators=(',', ':')).encode('utf-8')
    payload_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    
    unsigned_token = f"{_b64url_encode(header_bytes)}.{_b64url_encode(payload_bytes)}"
    signature = hmac.new(
        SECRET_KEY.encode('utf-8'),
        unsigned_token.encode('utf-8'),
        hashlib.sha256
    ).digest()
    
    return f"{unsigned_token}.{_b64url_encode(signature)}"

def decode_access_token(token: str) -> Optional[dict]:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        
        header_str, payload_str, signature_str = parts
        unsigned_token = f"{header_str}.{payload_str}"
        
        expected_sig = hmac.new(
            SECRET_KEY.encode('utf-8'),
            unsigned_token.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        actual_sig = _b64url_decode(signature_str)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
            
        payload = json.loads(_b64url_decode(payload_str).decode('utf-8'))
        if payload.get("exp", 0) < int(time.time()):
            return None  # Expired
            
        return payload
    except Exception:
        return None

# User Registration & Auth DB Queries
def register_user(email: str, password: str, full_name: str, role: str = "student", grade: int = 9) -> Dict[str, Any]:
    conn = sqlite3.connect(AUTH_DB_PATH)
    cursor = conn.cursor()
    
    user_id = f"usr_{os.urandom(8).hex()}"
    pwd_hash = hash_password(password)
    
    try:
        cursor.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, grade) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, email.lower().strip(), pwd_hash, full_name, role, grade)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="User with this email already exists.")
    
    conn.close()
    token = create_access_token({"sub": user_id, "email": email, "role": role, "grade": grade})
    return {
        "user_id": user_id,
        "email": email,
        "full_name": full_name,
        "role": role,
        "grade": grade,
        "access_token": token,
        "token_type": "bearer"
    }

def authenticate_user(email: str, password: str) -> Dict[str, Any]:
    conn = sqlite3.connect(AUTH_DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, password_hash, full_name, role, grade FROM users WHERE email = ?", (email.lower().strip(),))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not verify_password(password, row[1]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
        
    user_id, _, full_name, role, grade = row
    token = create_access_token({"sub": user_id, "email": email, "role": role, "grade": grade})
    return {
        "user_id": user_id,
        "email": email,
        "full_name": full_name,
        "role": role,
        "grade": grade,
        "access_token": token,
        "token_type": "bearer"
    }

# FastAPI Security Bearer Dependency
security_scheme = HTTPBearer(auto_error=False)

async def get_current_user_optional(credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme)) -> Optional[dict]:
    if not credentials:
        return None
    token = credentials.credentials
    return decode_access_token(token)

async def get_current_user_required(credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme)) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication token required.")
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    return payload

# User-Provided Cloud LLM Header Extraction Helper
def extract_user_llm_credentials(
    x_user_llm_key: Optional[str] = Header(None, alias="X-User-LLM-Key"),
    x_user_llm_provider: Optional[str] = Header(None, alias="X-User-LLM-Provider"),
    x_user_llm_model: Optional[str] = Header(None, alias="X-User-LLM-Model")
) -> Dict[str, Optional[str]]:
    return {
        "api_key": x_user_llm_key.strip() if x_user_llm_key else None,
        "provider": x_user_llm_provider.lower().strip() if x_user_llm_provider else "offline",
        "model": x_user_llm_model.strip() if x_user_llm_model else None
    }
