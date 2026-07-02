import hashlib
import hmac
import secrets
from contextvars import ContextVar
from typing import Any

from fastapi import HTTPException, Request, WebSocket

from .config import SESSION_TTL_HOURS

request_user: ContextVar[dict[str, Any] | None] = ContextVar("request_user", default=None)


def role_for_group(group_name: str) -> str:
    return "admin" if group_name in ("platform_admin", "admin", "teacher") else "member"


def is_admin_user(user: dict[str, Any] | None) -> bool:
    return bool(user) and (
        user.get("role") == "admin"
        or user.get("group_name") in ("platform_admin", "admin", "teacher")
    )


def normalize_user_role(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if user and user.get("group_name") in ("platform_admin", "admin", "teacher") and user.get("role") != "admin":
        user = dict(user)
        user["role"] = "admin"
    return user


def hash_password(password: str, encoded: str = "") -> str:
    if encoded:
        _, iterations, salt, _ = encoded.split("$", 3)
        rounds, salt_bytes = int(iterations), bytes.fromhex(salt)
    else:
        rounds, salt_bytes = 310_000, secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, rounds)
    return f"pbkdf2_sha256${rounds}${salt_bytes.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        return bool(encoded) and hmac.compare_digest(hash_password(password, encoded), encoded)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bearer_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def authenticate_token(conn, token: str, now: int) -> dict[str, Any] | None:
    if not token:
        return None
    hashed = token_hash(token)
    # 优先匹配会话 Token
    row = conn.execute(
        "SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=%s AND s.expires_at >= %s AND u.enabled=TRUE",
        (hashed, now),
    ).fetchone()
    if row:
        conn.execute("UPDATE auth_sessions SET last_seen_at=%s WHERE token_hash=%s", (now, hashed))
        return normalize_user_role(row)
    # 匹配 API Token（个人长效 token）
    api_row = conn.execute(
        "SELECT u.* FROM api_tokens t JOIN users u ON u.id=t.user_id "
        "WHERE t.token_hash=%s AND (t.expires_at=0 OR t.expires_at>=%s) AND u.enabled=TRUE",
        (hashed, now),
    ).fetchone()
    if api_row:
        conn.execute("UPDATE api_tokens SET last_used_at=%s WHERE token_hash=%s", (now, hashed))
        return normalize_user_role(api_row)
    return None


def create_session(conn, user_id: int, now: int) -> tuple[str, int]:
    token = secrets.token_urlsafe(48)
    expires = now + SESSION_TTL_HOURS * 3600
    conn.execute("INSERT INTO auth_sessions VALUES (%s,%s,%s,%s,%s)", (token_hash(token), user_id, now, expires, now))
    return token, expires


def require_user() -> dict[str, Any]:
    user = request_user.get()
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin() -> dict[str, Any]:
    user = require_user()
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


def websocket_token(websocket: WebSocket) -> str:
    header = websocket.headers.get("authorization", "")
    return websocket.query_params.get("token", "") or (header[7:].strip() if header.lower().startswith("bearer ") else "")
