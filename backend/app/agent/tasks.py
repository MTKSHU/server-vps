from typing import Any
import threading
import time as _time

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..core import hash_token, now_ts

# ---------------------------------------------------------------------------
# 节点任务完成事件注册表
#
# _wait_for_node_task 创建 Event 后等待；complete_node_task 回调时
# 调用 signal_node_task_done 唤醒等待方，避免持续轮询数据库。
# 使用 threading.Event 而非 asyncio.Event，因为 sync FastAPI 路由
# 运行在 starlette 的线程池中。
#
# 泄漏防护：
#   - _MAX_TASK_EVENTS：注册表上限。超限时拒绝新注册（等待方回退到
#     超时后 DB 读取），避免内存无界增长。
#   - _event_created_at：记录注册时间，周期性扫描清除已超 TTL 的孤儿 Event
#     （即请求在 _wait_for_node_task 拿到 event 之前就超时退出的情况）。
# ---------------------------------------------------------------------------
_node_task_events: dict[int, threading.Event] = {}
_event_created_at: dict[int, float] = {}   # task_id -> monotonic time
_node_task_events_lock = threading.Lock()
_MAX_TASK_EVENTS = 256          # 最多同时等待的任务数
_EVENT_ORPHAN_TTL = 120.0       # 孤儿 Event 最长保留时间（秒）
_last_gc_at: float = 0.0


def _gc_orphan_events() -> None:
    """清理超过 TTL 且已被 set 或长时间无人等待的孤儿 Event（在持锁外调用）。"""
    global _last_gc_at
    now = _time.monotonic()
    if now - _last_gc_at < 30:
        return
    _last_gc_at = now
    with _node_task_events_lock:
        expired = [
            tid for tid, created in _event_created_at.items()
            if now - created > _EVENT_ORPHAN_TTL
        ]
        for tid in expired:
            _node_task_events.pop(tid, None)
            _event_created_at.pop(tid, None)


def get_node_task_event(task_id: int) -> threading.Event:
    """注册并返回指定 node_task_id 的 Event（幂等）。
    注册表满时返回一个未注册的一次性 Event，等待方超时后从 DB 读取结果。
    """
    _gc_orphan_events()
    with _node_task_events_lock:
        if task_id in _node_task_events:
            return _node_task_events[task_id]
        if len(_node_task_events) >= _MAX_TASK_EVENTS:
            # 注册表已满，返回未注册的临时 Event；调用方在超时后会从 DB 读取
            return threading.Event()
        event = threading.Event()
        _node_task_events[task_id] = event
        _event_created_at[task_id] = _time.monotonic()
        return event


def signal_node_task_done(task_id: int) -> None:
    """唤醒等待该任务完成的调用方（best-effort，无等待方时静默）。"""
    with _node_task_events_lock:
        event = _node_task_events.get(task_id)
    if event is not None:
        event.set()


def release_node_task_event(task_id: int) -> None:
    """清理已使用的 Event，防止内存泄漏。"""
    with _node_task_events_lock:
        _node_task_events.pop(task_id, None)
        _event_created_at.pop(task_id, None)


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
