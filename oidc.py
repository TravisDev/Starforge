"""OIDC client: discovery, PKCE-protected auth-code flow, ID-token verification."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError
from fastapi import HTTPException

from auth import db, decrypt, encrypt, now_iso

STATE_TTL = timedelta(minutes=10)
_DISCOVERY_CACHE: dict[str, tuple[float, dict]] = {}
_JWKS_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 600  # seconds


async def _fetch_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


async def get_discovery(issuer: str) -> dict:
    now = time.time()
    cached = _DISCOVERY_CACHE.get(issuer)
    if cached and cached[0] > now:
        return cached[1]
    issuer_clean = issuer.rstrip("/")
    doc = await _fetch_json(f"{issuer_clean}/.well-known/openid-configuration")
    _DISCOVERY_CACHE[issuer] = (now + _CACHE_TTL, doc)
    return doc


async def get_jwks(jwks_uri: str):
    now = time.time()
    cached = _JWKS_CACHE.get(jwks_uri)
    if cached and cached[0] > now:
        return cached[1]
    raw = await _fetch_json(jwks_uri)
    key_set = JsonWebKey.import_key_set(raw)
    _JWKS_CACHE[jwks_uri] = (now + _CACHE_TTL, key_set)
    return key_set


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def list_enabled_providers() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, slug, display_name FROM sso_providers WHERE is_enabled = 1 ORDER BY display_name"
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_providers() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, slug, display_name, issuer, client_id, scopes, is_enabled,
                      created_at, updated_at
               FROM sso_providers ORDER BY display_name"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_provider_by_slug(slug: str) -> Optional[dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM sso_providers WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def get_provider_by_id(pid: int) -> Optional[dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM sso_providers WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def create_provider(
    slug: str,
    display_name: str,
    issuer: str,
    client_id: str,
    client_secret: str,
    scopes: str = "openid email profile",
    is_enabled: bool = True,
) -> int:
    enc = encrypt(client_secret)
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO sso_providers
               (slug, display_name, issuer, client_id, client_secret_enc, scopes, is_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, display_name, issuer.rstrip("/"), client_id, enc, scopes, 1 if is_enabled else 0, ts, ts),
        )
        return cur.lastrowid


def update_provider(pid: int, **fields) -> None:
    cols, params = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k == "client_secret":
            cols.append("client_secret_enc = ?")
            params.append(encrypt(v))
            continue
        if k == "is_enabled":
            v = 1 if v else 0
        if k == "issuer":
            v = v.rstrip("/")
        if k not in {"slug", "display_name", "issuer", "client_id", "scopes", "is_enabled"}:
            continue
        cols.append(f"{k} = ?")
        params.append(v)
    if not cols:
        return
    cols.append("updated_at = ?")
    params.append(now_iso())
    params.append(pid)
    with db() as conn:
        conn.execute(f"UPDATE sso_providers SET {', '.join(cols)} WHERE id = ?", params)


def delete_provider(pid: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sso_providers WHERE id = ?", (pid,))


# ---------- OIDC flow ----------

def _save_state(provider_id: int, state: str, nonce: str, verifier: str, return_to: str) -> None:
    now = datetime.now(timezone.utc)
    with db() as conn:
        conn.execute(
            """INSERT INTO oidc_states (state, provider_id, nonce, code_verifier, return_to, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (state, provider_id, nonce, verifier, return_to,
             now.isoformat(), (now + STATE_TTL).isoformat()),
        )
        conn.execute("DELETE FROM oidc_states WHERE expires_at < ?", (now.isoformat(),))


def _consume_state(state: str) -> Optional[dict[str, Any]]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM oidc_states WHERE state = ? AND expires_at > ?",
            (state, now_iso()),
        ).fetchone()
        conn.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
    return dict(row) if row else None


def _redirect_uri(request_url_base: str, slug: str) -> str:
    return f"{request_url_base.rstrip('/')}/auth/{slug}/callback"


async def begin_login(provider: dict, request_url_base: str, return_to: str) -> str:
    discovery = await get_discovery(provider["issuer"])
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    _save_state(provider["id"], state, nonce, verifier, return_to)
    params = {
        "client_id": provider["client_id"],
        "response_type": "code",
        "redirect_uri": _redirect_uri(request_url_base, provider["slug"]),
        "scope": provider["scopes"],
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{discovery['authorization_endpoint']}?{urlencode(params)}"


async def complete_login(
    slug: str,
    code: str,
    state: str,
    request_url_base: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Returns (provider, claims, return_to)."""
    saved = _consume_state(state)
    if not saved:
        raise HTTPException(400, "invalid or expired state")

    provider = get_provider_by_slug(slug)
    if not provider or provider["id"] != saved["provider_id"] or not provider["is_enabled"]:
        raise HTTPException(400, "unknown or disabled provider")

    discovery = await get_discovery(provider["issuer"])
    client_secret = decrypt(provider["client_secret_enc"])
    redirect_uri = _redirect_uri(request_url_base, slug)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": provider["client_id"],
                "client_secret": client_secret,
                "code_verifier": saved["code_verifier"],
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise HTTPException(400, f"token exchange failed: {resp.text[:200]}")
    tokens = resp.json()
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(400, "id_token missing from token response")

    jwks = await get_jwks(discovery["jwks_uri"])
    try:
        claims = jwt.decode(
            id_token,
            jwks,
            claims_options={
                "iss": {"essential": True, "value": provider["issuer"].rstrip("/")},
                "aud": {"essential": True, "value": provider["client_id"]},
                "exp": {"essential": True},
            },
        )
        claims.validate()
    except JoseError as e:
        raise HTTPException(400, f"id_token validation failed: {e}")

    if claims.get("nonce") != saved["nonce"]:
        raise HTTPException(400, "nonce mismatch")

    return provider, dict(claims), saved.get("return_to") or "/"


def find_or_create_user_for_claims(provider: dict, claims: dict) -> int:
    sub = str(claims.get("sub") or "")
    email = (claims.get("email") or "").strip().lower()
    name = claims.get("name") or claims.get("preferred_username") or email
    if not sub:
        raise HTTPException(400, "id_token missing sub claim")

    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM sso_identities WHERE provider_id = ? AND subject = ?",
            (provider["id"], sub),
        ).fetchone()
        if row:
            return row["user_id"]

        user_id = None
        if email:
            row = conn.execute(
                "SELECT id FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if row:
                user_id = row["id"]

        if user_id is None:
            if not email:
                raise HTTPException(400, "no email claim — cannot auto-provision user")
            cur = conn.execute(
                """INSERT INTO users (email, display_name, password_hash, is_admin, is_active, created_at)
                   VALUES (?, ?, NULL, 0, 1, ?)""",
                (email, name, now_iso()),
            )
            user_id = cur.lastrowid

        conn.execute(
            """INSERT INTO sso_identities (provider_id, user_id, subject, created_at)
               VALUES (?, ?, ?, ?)""",
            (provider["id"], user_id, sub, now_iso()),
        )
    return user_id
