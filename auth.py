"""Auth primitives: password hashing, AES-256-GCM at rest, session management."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Cookie, Depends, HTTPException, Request

ROOT = Path(__file__).parent
DATA_DIR = Path(os.environ.get("STARFORGE_DATA_DIR", str(ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "board.db"
KEY_PATH = DATA_DIR / "secret.key"

SESSION_COOKIE = "starforge_session"
SESSION_TTL = timedelta(days=30)
BEHIND_TLS = os.environ.get("BEHIND_TLS", "0") == "1"

_hasher = PasswordHasher()


# ---------- Key management ----------

def _load_or_create_key() -> bytes:
    env = os.environ.get("STARFORGE_KEY")
    if env:
        try:
            key = base64.urlsafe_b64decode(env + "=" * (-len(env) % 4))
        except Exception as e:
            raise RuntimeError("STARFORGE_KEY must be base64-encoded 32 bytes") from e
        if len(key) != 32:
            raise RuntimeError("STARFORGE_KEY must decode to 32 bytes")
        return key
    if KEY_PATH.exists():
        key = KEY_PATH.read_bytes()
        if len(key) != 32:
            raise RuntimeError(f"{KEY_PATH} is corrupt — expected 32 bytes")
        return key
    key = secrets.token_bytes(32)
    KEY_PATH.write_bytes(key)
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:
        pass
    return key


AES_KEY = _load_or_create_key()
_aes = AESGCM(AES_KEY)


def encrypt(plaintext: str) -> bytes:
    """AES-256-GCM. Output: 12-byte nonce || ciphertext || 16-byte tag."""
    nonce = secrets.token_bytes(12)
    return nonce + _aes.encrypt(nonce, plaintext.encode("utf-8"), None)


def decrypt(blob: bytes) -> str:
    nonce, ct = blob[:12], blob[12:]
    return _aes.decrypt(nonce, ct, None).decode("utf-8")


# ---------- DB helper ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_auth_schema() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                display_name TEXT NOT NULL DEFAULT '',
                password_hash TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                user_agent TEXT,
                ip TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

            CREATE TABLE IF NOT EXISTS sso_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                issuer TEXT NOT NULL,
                client_id TEXT NOT NULL,
                client_secret_enc BLOB NOT NULL,
                scopes TEXT NOT NULL DEFAULT 'openid email profile',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sso_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL REFERENCES sso_providers(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                subject TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(provider_id, subject)
            );

            CREATE TABLE IF NOT EXISTS oidc_states (
                state TEXT PRIMARY KEY,
                provider_id INTEGER NOT NULL,
                nonce TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                return_to TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


# ---------- Passwords ----------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


# ---------- Sessions ----------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, user_agent: Optional[str], ip: Optional[str]) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    with db() as conn:
        conn.execute(
            """INSERT INTO sessions
               (user_id, token_hash, created_at, expires_at, last_seen_at, user_agent, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                token_hash,
                now.isoformat(),
                (now + SESSION_TTL).isoformat(),
                now.isoformat(),
                (user_agent or "")[:300],
                (ip or "")[:64],
            ),
        )
    return token


def revoke_session(token: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def revoke_session_id(session_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def cleanup_sessions() -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))


def get_user_by_session(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    th = _hash_token(token)
    with db() as conn:
        row = conn.execute(
            """SELECT u.* FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1""",
            (th, now_iso()),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now_iso(), th),
            )
    return dict(row) if row else None


# ---------- User CRUD ----------

def user_count() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def create_user(
    email: str,
    display_name: str,
    password: Optional[str],
    is_admin: bool = False,
) -> int:
    pw_hash = hash_password(password) if password else None
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO users (email, display_name, password_hash, is_admin, is_active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (email.strip().lower(), display_name.strip(), pw_hash, 1 if is_admin else 0, now_iso()),
        )
        return cur.lastrowid


def touch_login(user_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso(), user_id))


# ---------- FastAPI dependencies ----------

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


async def current_user(
    request: Request,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    user = get_user_by_session(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    request.state.session_token = session_token
    return user


async def current_admin(user: dict = Depends(current_user)) -> dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return user


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=BEHIND_TLS,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def attach_request_meta(request: Request) -> tuple[str, str]:
    return request.headers.get("user-agent", ""), _client_ip(request)
