from typing import Any

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..core import hash_token, now_ts

def verify_agent_node(conn, token: str, hostname: str) -> dict[str, Any]:
    node = conn.execute("SELECT * FROM nodes WHERE hostname = %s", (hostname,)).fetchone()
    if not node or node["node_token"] != hash_token(token):
        raise HTTPException(status_code=403, detail="agent token 或 hostname 不匹配")
    return node

def enqueue_node_task(
    conn,
    node_id: int,
    container_id: int | None,
    task_type: str,
    payload: dict[str, Any],
    data_sync_task_id: int | None = None,
    available_at: int = 0,
) -> dict[str, Any]:
    ts = now_ts()
    return conn.execute(
        """
        INSERT INTO node_tasks (
            node_id, container_id, data_sync_task_id, task_type, payload, status, available_at, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s)
        RETURNING *
        """,
        (node_id, container_id, data_sync_task_id, task_type, Jsonb(payload), available_at, ts, ts),
    ).fetchone()

def public_task(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "node_id": row["node_id"],
        "container_id": row["container_id"],
        "data_sync_task_id": row["data_sync_task_id"],
        "type": row["task_type"],
        "status": row["status"],
        "attempts": row["attempts"],
        "error": row["last_error"],
        "result": row["result"],
        "created_at": row["created_at"],
        "claimed_at": row["claimed_at"],
        "finished_at": row["finished_at"],
        "available_at": row["available_at"],
        "updated_at": row["updated_at"],
    }
