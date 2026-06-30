import hashlib
import time
from contextlib import contextmanager
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import DATABASE_URL
from .auth import require_user

def now_ts() -> int:
    return int(time.time())

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@contextmanager
def db():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn

def audit(conn, actor: str, action: str, target: str, detail: dict[str, Any]):
    conn.execute(
        """
        INSERT INTO audit_logs (actor, action, target, detail, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (actor, action, target, Jsonb(detail), now_ts()),
    )

def current_user(conn) -> dict[str, Any]:
    return require_user()

def validate_platform_path(value: str, label: str) -> str:
    path = value.strip()
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail=f"{label} 必须是绝对路径")
    if "\x00" in path or "/../" in path or path.endswith("/.."):
        raise HTTPException(status_code=400, detail=f"{label} 不合法")
    if len(path) > 240:
        raise HTTPException(status_code=400, detail=f"{label} 过长")
    return path.rstrip("/") or "/"
