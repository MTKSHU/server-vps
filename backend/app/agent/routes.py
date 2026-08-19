import asyncio
import json
import re
import uuid
from typing import Any

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from ..auth import authenticate_token, is_admin_user, websocket_token
from psycopg.types.json import Jsonb
from ..schemas import AgentMetricsInput, AgentTaskClaim, AgentTaskProgress, AgentTaskResult, NodeRegistration
from ..platform_settings import get_agent_collection_config, get_platform_settings
from ..agent.tasks import signal_node_task_done


class AgentChannel:
    def __init__(self, node_id: int, websocket: WebSocket):
        self.node_id = node_id
        self.websocket = websocket
        self.send_lock = asyncio.Lock()


agent_channels: dict[int, AgentChannel] = {}
terminal_clients: dict[str, WebSocket] = {}
terminal_nodes: dict[str, int] = {}
terminal_lock = asyncio.Lock()


def register_agent_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]
    audit = deps["audit"]
    verify_agent_node = deps["verify_agent_node"]
    upsert_node = deps["upsert_node"]
    enqueue_node_task = deps["enqueue_node_task"]
    storage_root_for_node = deps["storage_root_for_node"]
    incus_image_import_payload = deps["incus_image_import_payload"]
    node_has_incus_image = deps["node_has_incus_image"]

    def shared_resource_verify_payload(conn, resource: dict[str, Any], source_path: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "resource_id": resource["id"],
            "resource_type": resource["resource_type"],
            "name": resource["name"],
            "version": resource["version"],
            "source_path": source_path,
        }
        match = re.fullmatch(r"(hf|ms)://([^@]+)@(.+)", str(resource.get("source_url") or ""))
        if not match:
            return payload
        source_kind, repo_id, revision = match.groups()
        payload.update(
            {
                "source": "huggingface" if source_kind == "hf" else "modelscope",
                "repo_id": repo_id,
                "revision": revision,
                "repo_type": "dataset" if resource["resource_type"] == "dataset" else "model",
            }
        )
        if source_kind == "hf":
            resource_endpoint = str(resource.get("source_endpoint") or "").strip()
            if resource_endpoint:
                payload["hf_endpoint"] = resource_endpoint
                return payload
            rows = conn.execute(
                "SELECT key, value FROM system_settings WHERE key IN ('hf_endpoint', 'hf_endpoint_enabled')"
            ).fetchall()
            settings = {row["key"]: row["value"] for row in rows}
            if settings.get("hf_endpoint_enabled") == "1":
                payload["hf_endpoint"] = settings.get("hf_endpoint", "")
        return payload

    def cleanup_container_sync_key(conn, task):
        payload = task["payload"] if isinstance(task["payload"], dict) else {}
        key_id = payload.get("sync_key_id")
        storage_node_id = int(payload.get("sync_storage_node_id") or 0)
        pubkey = payload.get("sync_pubkey", "")
        if not key_id or not storage_node_id:
            return
        try:
            enqueue_node_task(
                conn,
                storage_node_id,
                None,
                "remove_sync_pubkey",
                {"key_id": key_id, "public_key": pubkey},
                available_at=now_ts(),
            )
        except Exception:
            pass

    def enqueue_container_access_task(conn, task: dict[str, Any], ts: int):
        payload = task["payload"] if isinstance(task["payload"], dict) else {}
        container_id = int(task["container_id"] or payload.get("container_id") or 0)
        if not container_id:
            return None
        access_payload = {
            "container_id": container_id,
            "name": payload.get("name") or "",
            "ssh_username": payload.get("ssh_username") or "ubuntu",
            "ssh_key": payload.get("ssh_key") or "",
            "mounts": payload.get("mounts") or [],
        }
        if not access_payload["name"] or not access_payload["ssh_key"]:
            conn.execute(
                "UPDATE containers SET access_status = 'failed', access_error = %s, updated_at = %s WHERE id = %s",
                ("缺少 SSH 初始化参数", ts, container_id),
            )
            return None
        conn.execute(
            "UPDATE containers SET access_status = 'pending', access_error = '', updated_at = %s WHERE id = %s",
            (ts, container_id),
        )
        return enqueue_node_task(
            conn,
            task["node_id"],
            container_id,
            "incus_sync_ssh_keys",
            access_payload,
        )

    def task_payload_has_ssh_port(task: dict[str, Any]) -> bool:
        payload = task["payload"] if isinstance(task["payload"], dict) else {}
        for port in payload.get("ports") or []:
            if str(port.get("protocol") or "").lower() == "tcp" and int(port.get("container_port") or 0) == 22:
                return True
        return False

    def expire_stalled_container_sync_tasks(conn, node_id: int, ts: int) -> None:
        """Fail container_data_sync tasks that stopped sending lease heartbeats.

        container_data_sync should periodically call /progress while rsync is running.
        If claimed_at remains stale for too long, the execution chain is likely wedged.
        """
        stale_before = ts - 900
        stale_tasks = conn.execute(
            """
            SELECT *
            FROM node_tasks
            WHERE node_id = %s
              AND task_type = 'container_data_sync'
              AND status = 'claimed'
              AND claimed_at > 0
              AND claimed_at < %s
            FOR UPDATE SKIP LOCKED
            """,
            (node_id, stale_before),
        ).fetchall()
        for stale in stale_tasks:
            error = "container_data_sync stalled: progress heartbeat timeout (>900s); mark failed for safe retry"
            conn.execute(
                """
                UPDATE node_tasks
                SET status = 'failed',
                    last_error = %s,
                    finished_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (error, ts, ts, stale["id"]),
            )
            sync_task = None
            if stale["data_sync_task_id"]:
                sync_task = conn.execute(
                    "SELECT * FROM data_sync_tasks WHERE id = %s FOR UPDATE",
                    (stale["data_sync_task_id"],),
                ).fetchone()
                if sync_task and sync_task["status"] in ("planned", "running", "verifying", "retrying"):
                    conn.execute(
                        """
                        UPDATE data_sync_tasks
                        SET status = 'failed',
                            detail = detail || %s,
                            finished_at = %s,
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            Jsonb({
                                "error": error,
                                "status": "stalled-timeout",
                                "node_task_id": stale["id"],
                            }),
                            ts,
                            ts,
                            stale["data_sync_task_id"],
                        ),
                    )
                if sync_task and sync_task["task_type"] == "shared_resource_upload" and sync_task["resource_id"]:
                    conn.execute(
                        """
                        UPDATE shared_resources
                        SET request_status = 'failed',
                            check_status = 'failed',
                            check_error = %s,
                            updated_at = %s
                        WHERE id = %s
                          AND request_status IN ('uploading', 'finalizing', 'checking')
                        """,
                        (error, ts, sync_task["resource_id"]),
                    )
            cleanup_container_sync_key(conn, stale)
            audit(
                conn,
                "system",
                "expire-stalled-container-sync",
                f"node-task:{stale['id']}",
                {"node_id": node_id, "data_sync_task_id": stale.get("data_sync_task_id")},
            )

    @app.post("/api/nodes/register", status_code=201)
    def register_node(payload: NodeRegistration):
        with db() as conn:
            node = upsert_node(conn, payload)
            node["agent_config"] = get_agent_collection_config(conn)
            return node

    @app.post("/api/nodes/{node_id}/heartbeat")
    def heartbeat(node_id: int, payload: NodeRegistration):
        with db() as conn:
            existing = conn.execute("SELECT hostname FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="节点不存在")
            payload.hostname = existing["hostname"]
            node = upsert_node(conn, payload)
            expire_stalled_container_sync_tasks(conn, node["id"], now_ts())
            node["agent_config"] = get_agent_collection_config(conn)
            return node

    @app.post("/api/nodes/metrics")
    def report_node_metrics(payload: AgentMetricsInput):
        with db() as conn:
            node = verify_agent_node(conn, payload.token, payload.hostname)
            conn.execute(
                """
                UPDATE nodes SET
                    uptime_seconds = %s,
                    cpu_usage_percent = %s,
                    cpu_temperature_c = %s,
                    memory_total_gb = %s,
                    memory_used_gb = %s,
                    load_avg = %s,
                    swap_total_gb = %s,
                    swap_used_gb = %s,
                    network_interface = %s,
                    network_rx_bytes_per_sec = %s,
                    network_tx_bytes_per_sec = %s
                WHERE id = %s
                """,
                (
                    payload.uptime_seconds,
                    max(0, min(100, payload.cpu_usage_percent)),
                    payload.cpu_temperature_c,
                    max(1, payload.memory_total_gb),
                    max(0, payload.memory_used_gb),
                    payload.load_avg,
                    max(0, payload.swap_total_gb),
                    max(0, payload.swap_used_gb),
                    payload.network_interface.strip()[:64],
                    max(0, payload.network_rx_bytes_per_sec),
                    max(0, payload.network_tx_bytes_per_sec),
                    node["id"],
                ),
            )
            for gpu in payload.gpus:
                conn.execute(
                    """
                    UPDATE gpus SET
                        vram_used_mb = %s,
                        temperature_c = %s,
                        power_w = %s,
                        utilization = %s
                    WHERE node_id = %s AND uuid = %s
                    """,
                    (
                        max(0, gpu.vram_used_mb),
                        gpu.temperature_c,
                        max(0, gpu.power_w),
                        max(0, min(100, gpu.utilization)),
                        node["id"],
                        gpu.uuid,
                    ),
                )
            return {"ok": True}

    async def send_agent_message(node_id: int, message: dict[str, Any]):
        async with terminal_lock:
            channel = agent_channels.get(node_id)
        if not channel:
            raise HTTPException(status_code=409, detail="节点 agent terminal 通道未连接")
        async with channel.send_lock:
            await channel.websocket.send_json(message)

    @app.websocket("/api/agent/terminal")
    async def agent_terminal(websocket: WebSocket):
        authorization = websocket.headers.get("authorization", "")
        token = websocket.query_params.get("token", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        hostname = websocket.query_params.get("hostname", "")
        await websocket.accept()
        try:
            with db() as conn:
                node = verify_agent_node(conn, token, hostname)
            channel = AgentChannel(node["id"], websocket)
            async with terminal_lock:
                agent_channels[node["id"]] = channel
            await websocket.send_json({"type": "ready"})
            while True:
                message = await websocket.receive_json()
                session_id = message.get("session_id", "")
                if not session_id:
                    continue
                async with terminal_lock:
                    client = terminal_clients.get(session_id)
                if client:
                    await client.send_json(message)
        except WebSocketDisconnect:
            pass
        finally:
            async with terminal_lock:
                for node_id, channel in list(agent_channels.items()):
                    if channel.websocket is websocket:
                        agent_channels.pop(node_id, None)

    @app.websocket("/api/containers/{container_id}/terminal")
    async def container_terminal(websocket: WebSocket, container_id: int):
        await websocket.accept()
        session_id = uuid.uuid4().hex
        try:
            with db() as conn:
                user = authenticate_token(conn, websocket_token(websocket), now_ts())
                container = conn.execute(
                    """
                    SELECT c.*, n.hostname AS node
                    FROM containers c JOIN nodes n ON n.id = c.node_id
                    WHERE c.id = %s
                    """,
                    (container_id,),
                ).fetchone()
            if not user:
                await websocket.send_json({"type": "error", "error": "请先登录"})
                await websocket.close(code=4401)
                return
            if not container:
                await websocket.send_json({"type": "error", "error": "容器不存在"})
                await websocket.close()
                return
            if not is_admin_user(user) and container["owner_id"] != user["id"]:
                await websocket.send_json({"type": "error", "error": "只能访问自己的容器"})
                await websocket.close(code=4403)
                return
            if container["status"] != "running":
                await websocket.send_json({"type": "error", "error": "只有 running 容器可以打开终端"})
                await websocket.close()
                return
            if container.get("access_status") != "ready":
                await websocket.send_json({"type": "error", "error": "容器 SSH 初始化尚未完成"})
                await websocket.close()
                return
            async with terminal_lock:
                channel = agent_channels.get(container["node_id"])
                if not channel:
                    await websocket.send_json({"type": "error", "error": "节点 agent terminal 通道未连接"})
                    await websocket.close()
                    return
                terminal_clients[session_id] = websocket
                terminal_nodes[session_id] = container["node_id"]
            cols = int(websocket.query_params.get("cols", "100"))
            rows = int(websocket.query_params.get("rows", "32"))
            await send_agent_message(
                container["node_id"],
                {
                    "type": "start",
                    "session_id": session_id,
                    "container": container["name"],
                    "user": container["ssh_username"],
                    "cols": cols,
                    "rows": rows,
                },
            )
            audit_detail = {
                "container": container["name"],
                "node": container["node"],
                "user": container["ssh_username"],
            }
            with db() as conn:
                audit(conn, user["username"], "terminal-open", f"container:{container_id}", audit_detail)
            while True:
                message = await websocket.receive_json()
                message["session_id"] = session_id
                await send_agent_message(container["node_id"], message)
        except WebSocketDisconnect:
            pass
        finally:
            async with terminal_lock:
                node_id = terminal_nodes.pop(session_id, None)
                terminal_clients.pop(session_id, None)
            if node_id:
                try:
                    await send_agent_message(node_id, {"type": "close", "session_id": session_id})
                except Exception:
                    pass

    @app.websocket("/api/nodes/{node_id}/terminal")
    async def node_terminal(websocket: WebSocket, node_id: int):
        await websocket.accept()
        session_id = uuid.uuid4().hex
        try:
            with db() as conn:
                user = authenticate_token(conn, websocket_token(websocket), now_ts())
                node = conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not user:
                await websocket.send_json({"type": "error", "error": "请先登录"})
                await websocket.close(code=4401)
                return
            if not is_admin_user(user):
                await websocket.send_json({"type": "error", "error": "仅管理员可访问节点终端"})
                await websocket.close(code=4403)
                return
            if not node:
                await websocket.send_json({"type": "error", "error": "节点不存在"})
                await websocket.close()
                return
            cols = int(websocket.query_params.get("cols", "100"))
            rows = int(websocket.query_params.get("rows", "32"))
            async with terminal_lock:
                channel = agent_channels.get(node_id)
                if not channel:
                    await websocket.send_json({"type": "error", "error": "节点 agent terminal 通道未连接"})
                    await websocket.close()
                    return
                terminal_clients[session_id] = websocket
                terminal_nodes[session_id] = node_id
            await send_agent_message(
                node_id,
                {
                    "type": "start",
                    "session_id": session_id,
                    "container": "",
                    "cols": cols,
                    "rows": rows,
                },
            )
            with db() as conn:
                audit(conn, user["username"], "terminal-open", f"node:{node_id}", {"node": node["hostname"], "transport": "agent"})
            while True:
                message = await websocket.receive_json()
                message["session_id"] = session_id
                await send_agent_message(node_id, message)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            async with terminal_lock:
                connected_node_id = terminal_nodes.pop(session_id, None)
                terminal_clients.pop(session_id, None)
            if connected_node_id:
                try:
                    await send_agent_message(connected_node_id, {"type": "close", "session_id": session_id})
                except Exception:
                    pass

    @app.post("/api/nodes/tasks/claim")
    def claim_node_task(payload: AgentTaskClaim):
        with db() as conn:
            node = verify_agent_node(conn, payload.token, payload.hostname)
            stale_claimed_before = now_ts() - 300
            task = conn.execute(
                """
                SELECT * FROM node_tasks
                WHERE node_id = %s
                  AND available_at <= %s
                  AND (
                    status = 'pending'
                    OR (status = 'claimed' AND claimed_at < %s)
                  )
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (node["id"], now_ts(), stale_claimed_before),
            ).fetchone()
            if not task:
                return {"task": None}
            ts = now_ts()
            task = conn.execute(
                """
                UPDATE node_tasks
                SET status = 'claimed', claimed_at = %s, attempts = attempts + 1, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (ts, ts, task["id"]),
            ).fetchone()
            if task["data_sync_task_id"]:
                sync_status = "verifying" if task["task_type"] == "verify_data_sync" else "running"
                conn.execute(
                    "UPDATE data_sync_tasks SET status = %s, updated_at = %s WHERE id = %s",
                    (sync_status, ts, task["data_sync_task_id"]),
                )
            return {
                "task": {
                    "id": task["id"],
                    "type": task["task_type"],
                    "payload": task["payload"],
                    "attempts": task["attempts"],
                }
            }

    @app.post("/api/nodes/tasks/{task_id}/progress")
    def report_node_task_progress(task_id: int, payload: AgentTaskProgress):
        """agent 在执行 container_data_sync 期间周期性上报 rsync 传输进度。"""
        with db() as conn:
            node = verify_agent_node(conn, payload.token, payload.hostname)
            task = conn.execute(
                "SELECT data_sync_task_id, task_type, payload FROM node_tasks WHERE id = %s AND node_id = %s",
                (task_id, node["id"]),
            ).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="任务不存在")
            ts = now_ts()
            # Progress is also the execution lease heartbeat. Long-running
            # downloads can spend well over five minutes listing a large repo;
            # without renewing claimed_at the same task is reclaimed and two
            # downloaders corrupt the shared staging directory.
            conn.execute(
                """
                UPDATE node_tasks
                SET claimed_at = %s, updated_at = %s
                WHERE id = %s AND node_id = %s AND status = 'claimed'
                """,
                (ts, ts, task_id, node["id"]),
            )
            if task["data_sync_task_id"]:
                # 仅在任务尚未结束（planned/running）时更新，避免覆盖已完成状态
                conn.execute(
                    """
                    UPDATE data_sync_tasks
                    SET progress = %s, updated_at = %s
                    WHERE id = %s AND status IN ('planned', 'running')
                    """,
                    (Jsonb(payload.progress or {}), ts, task["data_sync_task_id"]),
                )
            if task["task_type"] == "download_shared_resource":
                task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                resource_id = int(task_payload.get("resource_id") or 0)
                if resource_id:
                    progress = dict(payload.progress or {})
                    progress.setdefault("task_id", task_id)
                    progress.setdefault("node", node["hostname"])
                    conn.execute(
                        """
                        UPDATE shared_resources
                        SET download_progress = %s, updated_at = %s
                        WHERE id = %s AND request_status = 'downloading'
                        """,
                        (Jsonb(progress), ts, resource_id),
                    )
        return {"ok": True}

    @app.post("/api/nodes/tasks/{task_id}/result")
    def complete_node_task(task_id: int, payload: AgentTaskResult):
        with db() as conn:
            node = verify_agent_node(conn, payload.token, payload.hostname)
            task = conn.execute("SELECT * FROM node_tasks WHERE id = %s AND node_id = %s", (task_id, node["id"])).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="任务不存在")
            ts = now_ts()
            if payload.ok:
                conn.execute(
                    """
                    UPDATE node_tasks
                    SET status = 'succeeded', last_error = '', result = %s, finished_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (Jsonb({"output": payload.output, "status": payload.status}), ts, ts, task_id),
                )
                if task["task_type"] == "incus_create_container" and task["container_id"]:
                    ip = payload.ip.strip()
                    if ip:
                        conn.execute(
                            "UPDATE containers SET status = 'running', ip = %s, updated_at = %s WHERE id = %s",
                            (ip, ts, task["container_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE containers SET status = 'running', updated_at = %s WHERE id = %s",
                            (ts, task["container_id"]),
                        )
                    access_task = enqueue_container_access_task(conn, task, ts)
                    if access_task:
                        audit(
                            conn,
                            "node-agent",
                            "task-created",
                            f"node-task:{access_task['id']}",
                            {"type": "incus_sync_ssh_keys", "container_id": task["container_id"]},
                        )
                if task["task_type"] == "incus_sync_ssh_keys" and task["container_id"]:
                    conn.execute(
                        "UPDATE containers SET access_status = 'ready', access_error = '', updated_at = %s WHERE id = %s",
                        (ts, task["container_id"]),
                    )
                if task["task_type"] == "migrate_container_home" and task["container_id"]:
                    migration_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    managed_mount = migration_payload.get("managed_mount") or {}
                    container_row = conn.execute("SELECT managed_mounts,mounts FROM containers WHERE id=%s", (task["container_id"],)).fetchone()
                    if container_row and managed_mount:
                        managed = [item for item in (container_row.get("managed_mounts") or []) if item.get("kind") != "user_home"]
                        managed.append(managed_mount)
                        legacy = [item for item in (container_row.get("mounts") or []) if f":{managed_mount.get('target')}" not in item]
                        suffix = ":ro" if managed_mount.get("readonly") else ":rw"
                        legacy.append(f"{managed_mount.get('source')}:{managed_mount.get('target')}{suffix}")
                        conn.execute("UPDATE containers SET managed_mounts=%s,mounts=%s,updated_at=%s WHERE id=%s",
                                     (Jsonb(managed), Jsonb(legacy), ts, task["container_id"]))
                if task["task_type"] == "incus_sync_ports" and task["container_id"] and task_payload_has_ssh_port(task):
                    conn.execute(
                        "UPDATE containers SET access_status = 'ready', access_error = '', updated_at = %s WHERE id = %s",
                        (ts, task["container_id"]),
                    )
                if task["task_type"] in ("incus_start_container", "incus_restart_container") and task["container_id"]:
                    ip = payload.ip.strip()
                    if ip:
                        conn.execute(
                            "UPDATE containers SET status = 'running', ip = %s, updated_at = %s WHERE id = %s",
                            (ip, ts, task["container_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE containers SET status = 'running', updated_at = %s WHERE id = %s",
                            (ts, task["container_id"]),
                        )
                if task["task_type"] == "incus_stop_container" and task["container_id"]:
                    conn.execute(
                        "UPDATE containers SET status = 'stopped', ip = '', updated_at = %s WHERE id = %s",
                        (ts, task["container_id"]),
                    )
                if task["task_type"] == "incus_delete_container" and task["container_id"]:
                    deleted_container = conn.execute(
                        "SELECT owner_id,node_id FROM containers WHERE id=%s", (task["container_id"],)
                    ).fetchone()
                    if deleted_container:
                        other_count = conn.execute(
                            "SELECT COUNT(*) AS count FROM containers WHERE owner_id=%s AND node_id=%s AND id<>%s AND status!='deleting'",
                            (deleted_container["owner_id"], deleted_container["node_id"], task["container_id"]),
                        ).fetchone()["count"]
                        if other_count == 0:
                            retention = int(get_platform_settings(conn)["workspace_retention_days"])
                            conn.execute(
                                "UPDATE user_workspace_volumes SET cleanup_after=%s,last_used_at=%s,updated_at=%s "
                                "WHERE user_id=%s AND node_id=%s AND lifecycle='temporary'",
                                (ts + retention * 86400, ts, ts, deleted_container["owner_id"], deleted_container["node_id"]),
                            )
                    conn.execute("UPDATE node_tasks SET container_id = NULL WHERE id = %s", (task_id,))
                    conn.execute("DELETE FROM containers WHERE id = %s", (task["container_id"],))
                if task["task_type"] == "download_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    target_path = str(task_payload.get("target_path") or "")
                    if resource_id:
                        selected_source = ""
                        if task_payload.get("source") == "priority":
                            selected_source = "modelscope" if "[ok] downloaded from ModelScope" in payload.output else "huggingface"
                        progress = {
                            "phase": "done",
                            "pct": 100,
                            "current_file": "",
                            "task_id": task_id,
                            "node": node["hostname"],
                            "selected_source": selected_source,
                        }
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET request_status = 'checking',
                                check_status = 'checking',
                                check_error = '',
                                download_progress = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (Jsonb(progress), ts, resource_id),
                        )
                        resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
                        if resource and target_path:
                            verify_payload = shared_resource_verify_payload(conn, resource, target_path)
                            for key in ("source", "repo_id", "revision", "token", "repo_type", "hf_endpoint"):
                                if task_payload.get(key):
                                    verify_payload[key] = task_payload[key]
                            if task_payload.get("source") == "priority":
                                # 优先链路无论命中哪个镜像，最终以公开 HF 仓库清单做完整性校验。
                                verify_payload["source"] = "huggingface"
                                verify_payload["repo_id"] = task_payload.get("fallback_repo_id") or task_payload.get("repo_id")
                                verify_payload["revision"] = task_payload.get("fallback_revision") or "main"
                            verify_task = enqueue_node_task(
                                conn,
                                task["node_id"],
                                None,
                                "verify_shared_resource",
                                verify_payload,
                                available_at=ts + 30,
                            )
                            audit(
                                conn,
                                "system",
                                "auto-verify",
                                f"shared-resource:{resource_id}",
                                {"node": node["hostname"], "path": target_path, "task_id": verify_task["id"]},
                            )
                if task["task_type"] == "prepare_shared_resource_download":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        progress = {
                            "phase": "manual_ready",
                            "pct": 0,
                            "task_id": task_id,
                            "node": node["hostname"],
                            "container_id": int(task_payload.get("container_id") or 0),
                            "container_name": "cluster-resource-downloader",
                            "container_path": "/srv/resource-staging",
                            "target_path": str(task_payload.get("target_path") or ""),
                            "manual_command": str(task_payload.get("manual_command") or ""),
                        }
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET request_status = 'awaiting_manual_download',
                                check_status = 'unknown',
                                check_error = '',
                                download_progress = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (Jsonb(progress), ts, resource_id),
                        )
                if task["task_type"] == "migrate_shared_resource_path":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    new_source_path = str(task_payload.get("new_source_path") or "")
                    new_path = str(task_payload.get("new_path") or "")
                    old_source_path = str(task_payload.get("old_source_path") or "")
                    if resource_id and new_source_path:
                        progress = {
                            "phase": "migrated",
                            "pct": 100,
                            "current_file": "",
                            "task_id": task_id,
                            "node": node["hostname"],
                            "from": old_source_path,
                            "to": new_source_path,
                        }
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET source_path = %s,
                                check_status = 'checking',
                                check_error = '',
                                download_progress = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (new_source_path, Jsonb(progress), ts, resource_id),
                        )
                        conn.execute("DELETE FROM shared_resource_scans WHERE resource_id = %s", (resource_id,))
                        resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
                        if resource and new_path:
                            verify_payload = shared_resource_verify_payload(conn, resource, new_path)
                            verify_task = enqueue_node_task(
                                conn,
                                task["node_id"],
                                None,
                                "verify_shared_resource",
                                verify_payload,
                                available_at=ts + 30,
                            )
                            audit(
                                conn,
                                "system",
                                "auto-verify",
                                f"shared-resource:{resource_id}",
                                {"node": node["hostname"], "path": new_path, "task_id": verify_task["id"]},
                            )
                if task["task_type"] == "incus_image_export":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    storage_image_file_id = int(task_payload.get("storage_image_file_id") or 0)
                    push_to_storage = task_payload.get("push_to_storage") or {}
                    result_detail: dict[str, Any] = {}
                    if payload.output.strip():
                        try:
                            parsed = json.loads(payload.output)
                            if isinstance(parsed, dict):
                                result_detail = parsed
                        except json.JSONDecodeError:
                            result_detail = {}
                    if push_to_storage:
                        # 导出完成但文件还在计算节点，下发推送任务将其迁移到存储节点
                        # storage_image_files 状态保持 'pending'，待推送完成后再更新
                        enqueue_node_task(
                            conn,
                            task["node_id"],  # 计算节点执行推送
                            None,
                            "incus_image_push_to_storage",
                            push_to_storage,
                        )
                    elif storage_image_file_id:
                        # 常规导出（直接落存储节点），标记为已导出
                        conn.execute(
                            """
                            UPDATE storage_image_files
                            SET status = 'exported',
                                size_bytes = %s,
                                last_error = '',
                                exported_at = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                int(result_detail.get("size_bytes") or 0),
                                ts,
                                ts,
                                storage_image_file_id,
                            ),
                        )
                        # export 任务携带 distribute_to_node_ids 时自动下发 import 任务
                        distribute_to = [
                            int(nid) for nid in (task_payload.get("distribute_to_node_ids") or [])
                            if nid
                        ]
                        if distribute_to:
                            sf = conn.execute(
                                "SELECT * FROM storage_image_files WHERE id = %s",
                                (storage_image_file_id,),
                            ).fetchone()
                            src_node = conn.execute(
                                "SELECT * FROM nodes WHERE id = %s",
                                (sf["source_node_id"],),
                            ).fetchone() if sf else None
                            if sf and src_node:
                                aliases_list = [
                                    a.strip() for a in (sf["aliases"] or "").split(",") if a.strip()
                                ]
                                import_alias = aliases_list[0] if aliases_list else sf["fingerprint"][:16]
                                for tgt_id in distribute_to:
                                    tgt_node = conn.execute(
                                        "SELECT * FROM nodes WHERE id = %s AND status = 'online'",
                                        (tgt_id,),
                                    ).fetchone()
                                    if not tgt_node:
                                        continue
                                    if node_has_incus_image(conn, tgt_id, import_alias):
                                        continue
                                    tgt_root = storage_root_for_node(conn, tgt_id)
                                    enqueue_node_task(
                                        conn,
                                        tgt_id,
                                        None,
                                        "incus_image_import",
                                        incus_image_import_payload(
                                            dict(sf),
                                            dict(src_node),
                                            dict(tgt_node),
                                            f"{tgt_root}/incus-images/import-cache/{sf['base_name']}",
                                            import_alias,
                                        ),
                                    )
                if task["task_type"] == "incus_image_push_to_storage":
                    # 推送完成：更新 storage_image_files 状态，清理计算节点临时文件，下发 import
                    push_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    storage_image_file_id = int(push_payload.get("storage_image_file_id") or 0)
                    compute_node_id = int(push_payload.get("compute_node_id") or 0)
                    compute_export_dir = (push_payload.get("compute_export_dir") or "").strip()
                    base_name = (push_payload.get("base_name") or "").strip()
                    distribute_to = [
                        int(nid) for nid in (push_payload.get("distribute_to_node_ids") or []) if nid
                    ]
                    if storage_image_file_id:
                        conn.execute(
                            """
                            UPDATE storage_image_files
                            SET status = 'exported',
                                last_error = '',
                                exported_at = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (ts, ts, storage_image_file_id),
                        )
                    # 清理计算节点上的临时导出目录
                    if compute_node_id and compute_export_dir and base_name:
                        try:
                            enqueue_node_task(
                                conn, compute_node_id, None, "incus_image_cleanup",
                                {"export_dir": compute_export_dir, "base_name": base_name, "fingerprint": ""},
                            )
                        except Exception:
                            pass
                    # 下发 import 任务到目标节点（从存储节点拉取）
                    if distribute_to and storage_image_file_id:
                        sf = conn.execute(
                            "SELECT * FROM storage_image_files WHERE id = %s",
                            (storage_image_file_id,),
                        ).fetchone()
                        src_node = conn.execute(
                            "SELECT * FROM nodes WHERE id = %s", (sf["source_node_id"],)
                        ).fetchone() if sf else None
                        if sf and src_node:
                            aliases_list = [a.strip() for a in (sf["aliases"] or "").split(",") if a.strip()]
                            import_alias = aliases_list[0] if aliases_list else sf["fingerprint"][:16]
                            for tgt_id in distribute_to:
                                tgt_node = conn.execute(
                                    "SELECT * FROM nodes WHERE id = %s AND status = 'online'", (tgt_id,)
                                ).fetchone()
                                if not tgt_node:
                                    continue
                                if node_has_incus_image(conn, tgt_id, import_alias):
                                    continue
                                tgt_root = storage_root_for_node(conn, tgt_id)
                                enqueue_node_task(
                                    conn, tgt_id, None, "incus_image_import",
                                    incus_image_import_payload(
                                        dict(sf), dict(src_node), dict(tgt_node),
                                        f"{tgt_root}/incus-images/import-cache/{sf['base_name']}",
                                        import_alias,
                                    ),
                                )
                if task["task_type"] == "incus_publish_container":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    storage_image_file_id = int(task_payload.get("storage_image_file_id") or 0)
                    result_detail: dict[str, Any] = {}
                    if payload.output.strip():
                        for line in reversed(payload.output.splitlines()):
                            try:
                                parsed = json.loads(line)
                                if isinstance(parsed, dict):
                                    result_detail = parsed
                                    break
                            except json.JSONDecodeError:
                                continue
                    if storage_image_file_id:
                        conn.execute(
                            """
                            UPDATE storage_image_files
                            SET status = 'exported',
                                size_bytes = %s,
                                last_error = '',
                                exported_at = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                int(result_detail.get("size_bytes") or 0),
                                ts,
                                ts,
                                storage_image_file_id,
                            ),
                        )
                if task["task_type"] == "ensure_user_zfs_dataset":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    user_id = int(task_payload.get("user_id") or 0)
                    result_detail: dict[str, Any] = {}
                    if payload.output.strip():
                        try:
                            parsed = json.loads(payload.output)
                            if isinstance(parsed, dict):
                                result_detail = parsed
                        except json.JSONDecodeError:
                            result_detail = {}
                    if user_id:
                        conn.execute(
                            """
                            UPDATE user_storage_datasets
                            SET dataset_name = %s,
                                mountpoint = %s,
                                quota_gb = %s,
                                nfs_share_id = %s,
                                nfs_export_path = %s,
                                nfs_share_status = %s,
                                status = 'applied',
                                last_error = '',
                                applied_at = %s,
                                updated_at = %s
                            WHERE user_id = %s
                            """,
                            (
                                str(result_detail.get("dataset_name") or task_payload.get("dataset_name") or ""),
                                str(result_detail.get("mountpoint") or task_payload.get("mountpoint") or ""),
                                int(result_detail.get("quota_gb") or task_payload.get("quota_gb") or 0),
                                int(result_detail.get("nfs_share_id") or 0),
                                str(result_detail.get("nfs_export_path") or result_detail.get("mountpoint") or task_payload.get("mountpoint") or ""),
                                str(result_detail.get("nfs_share_status") or "manual"),
                                ts,
                                ts,
                                user_id,
                            ),
                        )
                if task["task_type"] == "remove_user_zfs_dataset":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    user_id = int(task_payload.get("user_id") or 0)
                    if user_id:
                        conn.execute(
                            "DELETE FROM user_storage_datasets WHERE user_id = %s",
                            (user_id,),
                        )
                if task["task_type"] == "remove_user_workspace_volume":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    user_id = int(task_payload.get("user_id") or 0)
                    node_id = int(task_payload.get("node_id") or 0)
                    if user_id and node_id:
                        conn.execute(
                            """
                            UPDATE user_workspace_volumes
                            SET status = 'removed', last_error = '', removed_at = %s, updated_at = %s
                            WHERE user_id = %s AND node_id = %s
                            """,
                            (ts, ts, user_id, node_id),
                        )
                if task["task_type"] == "verify_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    result_detail: dict[str, Any] = {}
                    if payload.output.strip():
                        try:
                            parsed = json.loads(payload.output)
                            if isinstance(parsed, dict):
                                result_detail = parsed
                        except json.JSONDecodeError:
                            result_detail = {}
                    if resource_id:
                        progress_patch = Jsonb({"phase": "archived", "pct": 100}) if task_payload.get("manual_finalize") else None
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET request_status = 'ready',
                                check_status = 'ok',
                                size_bytes = %s,
                                file_count = %s,
                                check_error = '',
                                checked_at = %s,
                                download_progress = CASE WHEN %s::jsonb IS NULL THEN download_progress ELSE download_progress || %s END,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                int(result_detail.get("size_bytes") or 0),
                                int(result_detail.get("file_count") or 0),
                                ts,
                                progress_patch,
                                progress_patch,
                                ts,
                                resource_id,
                            ),
                        )
                if task["task_type"] == "sync_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    local_cache_path = str(task_payload.get("local_cache_path") or "")
                    if resource_id and local_cache_path:
                        conn.execute(
                            """
                            UPDATE node_resource_cache
                            SET status = 'ready',
                                local_path = %s,
                                synced_at = %s,
                                error = '',
                                updated_at = %s
                            WHERE node_id = %s AND resource_id = %s
                            """,
                            (local_cache_path, ts, ts, task["node_id"], resource_id),
                        )
                        # 同步完成后自动将该节点上所有安装了此资源的容器热更新节点挂载
                        resource_row = conn.execute(
                            "SELECT source_path, mount_path FROM shared_resources WHERE id = %s",
                            (resource_id,),
                        ).fetchone()
                        if resource_row:
                            old_source = resource_row["source_path"]
                            new_mount_path = resource_row["mount_path"]  # 当前（已标准化的）容器挂载点
                            containers_on_node = conn.execute(
                                "SELECT id, name, mounts FROM containers"
                                " WHERE node_id = %s AND status IN ('running', 'stopped')",
                                (task["node_id"],),
                            ).fetchall()
                            # 已处理容器 id 集合，避免同一容器处理两次
                            handled_ids: set[int] = set()
                            for ctr in containers_on_node:
                                cur_mounts = list(ctr.get("mounts") or [])
                                new_mounts = []
                                mount_updates = []
                                changed = False
                                for m in cur_mounts:
                                    readonly = m.endswith(":ro")
                                    core = m[:-3] if readonly else m
                                    parts = core.split(":", 1)
                                    src = parts[0]
                                    mpt = parts[1] if len(parts) == 2 else src
                                    # 匹配：源路径是存储节点路径，或挂载目标已经是本资源的容器挂载点
                                    # 后者涵盖了"上次同步已使用本地缓存路径"的情况，避免重复追加挂载条目
                                    if src == old_source or mpt == new_mount_path:
                                        sfx = ":ro" if readonly else ""
                                        new_mounts.append(f"{local_cache_path}:{new_mount_path}{sfx}")
                                        mount_updates.append({
                                            "old_target": mpt,
                                            "new_source": local_cache_path,
                                            "new_target": new_mount_path,
                                            "readonly": readonly,
                                        })
                                        changed = True
                                    else:
                                        new_mounts.append(m)
                                if changed:
                                    conn.execute(
                                        "UPDATE containers SET mounts = %s, updated_at = %s WHERE id = %s",
                                        (Jsonb(new_mounts), ts, ctr["id"]),
                                    )
                                    enqueue_node_task(
                                        conn, task["node_id"], ctr["id"],
                                        "apply_resource_mounts",
                                        {"container_id": ctr["id"], "name": ctr["name"], "mount_updates": mount_updates},
                                    )
                                    handled_ids.add(ctr["id"])
                            # 若本次同步关联了特定容器，且该容器尚无此资源挂载，主动添加
                            specific_ctr_id = task.get("container_id")
                            if specific_ctr_id and specific_ctr_id not in handled_ids:
                                specific_ctr = conn.execute(
                                    "SELECT id, name, mounts, status FROM containers WHERE id = %s",
                                    (specific_ctr_id,),
                                ).fetchone()
                                if specific_ctr and specific_ctr["status"] in ("running", "stopped"):
                                    existing_mounts = list(specific_ctr.get("mounts") or [])
                                    # 防止重复追加：检查容器挂载列表中是否已有以 new_mount_path 为目标的条目
                                    def _mount_target(mount_str: str) -> str:
                                        ro = mount_str.endswith(":ro")
                                        c = mount_str[:-3] if ro else mount_str
                                        p = c.split(":", 1)
                                        return p[1] if len(p) == 2 else p[0]
                                    existing_targets = {_mount_target(m) for m in existing_mounts}
                                    if new_mount_path not in existing_targets:
                                        new_mounts_for_ctr = existing_mounts + [
                                            f"{local_cache_path}:{new_mount_path}:ro"
                                        ]
                                        conn.execute(
                                            "UPDATE containers SET mounts = %s, updated_at = %s WHERE id = %s",
                                            (Jsonb(new_mounts_for_ctr), ts, specific_ctr_id),
                                        )
                                    enqueue_node_task(
                                        conn, task["node_id"], specific_ctr_id,
                                        "apply_resource_mounts",
                                        {
                                            "container_id": specific_ctr_id,
                                            "name": specific_ctr["name"],
                                            "mount_updates": [{
                                                "old_target": "",
                                                "new_source": local_cache_path,
                                                "new_target": new_mount_path,
                                                "readonly": True,
                                            }],
                                        },
                                    )
                if task["task_type"] == "scan_user_directory":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    result_detail: dict[str, Any] = {}
                    try:
                        parsed = json.loads(payload.output) if payload.output.strip() else {}
                        if isinstance(parsed, dict):
                            result_detail = parsed
                    except json.JSONDecodeError:
                        result_detail = {}
                    user_id = int(task_payload.get("user_id") or 0)
                    relative_path = str(task_payload.get("relative_path") or "")
                    if user_id:
                        conn.execute(
                            """
                            UPDATE user_directory_scans
                            SET status = 'ready', file_count = %s, size_bytes = %s,
                                entries = %s, truncated = %s, error = '',
                                scanned_at = %s, updated_at = %s
                            WHERE user_id = %s AND relative_path = %s
                            """,
                            (
                                int(result_detail.get("file_count") or 0),
                                int(result_detail.get("size_bytes") or 0),
                                Jsonb(result_detail.get("entries") or []),
                                bool(result_detail.get("truncated")),
                                ts, ts, user_id, relative_path,
                            ),
                        )
                if task["task_type"] == "scan_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    result_detail: dict[str, Any] = {}
                    try:
                        parsed = json.loads(payload.output) if payload.output.strip() else {}
                        if isinstance(parsed, dict):
                            result_detail = parsed
                    except json.JSONDecodeError:
                        result_detail = {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    relative_path = str(task_payload.get("relative_path") or "")
                    if resource_id:
                        conn.execute(
                            """
                            UPDATE shared_resource_scans
                            SET status = 'ready', file_count = %s, size_bytes = %s,
                                entries = %s, truncated = %s, error = '',
                                scanned_at = %s, updated_at = %s
                            WHERE resource_id = %s AND relative_path = %s
                            """,
                            (
                                int(result_detail.get("file_count") or 0),
                                int(result_detail.get("size_bytes") or 0),
                                Jsonb(result_detail.get("entries") or []),
                                bool(result_detail.get("truncated")),
                                ts, ts, resource_id, relative_path,
                            ),
                        )
                if task["data_sync_task_id"]:
                    sync_task = conn.execute(
                        "SELECT * FROM data_sync_tasks WHERE id = %s",
                        (task["data_sync_task_id"],),
                    ).fetchone()
                    verification = str((sync_task or {}).get("detail", {}).get("verification", "")).strip()
                    if sync_task and task["task_type"] != "verify_data_sync" and verification in ("size", "sha256", "manifest"):
                        verify_task = enqueue_node_task(
                            conn,
                            sync_task["target_node_id"],
                            sync_task["container_id"],
                            "verify_data_sync",
                            {
                                "sync_task_id": sync_task["id"],
                                "target_path": sync_task["target_path"],
                                "verification": verification,
                            },
                            data_sync_task_id=sync_task["id"],
                        )
                        conn.execute(
                            """
                            UPDATE data_sync_tasks
                            SET status = 'verifying', detail = detail || %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (Jsonb({"sync_output": payload.output, "verify_node_task_id": verify_task["id"]}), ts, sync_task["id"]),
                        )
                    else:
                        verification_result: dict[str, Any] = {}
                        if task["task_type"] == "verify_data_sync" and payload.output.strip():
                            try:
                                parsed = json.loads(payload.output)
                                if isinstance(parsed, dict):
                                    verification_result = parsed
                            except json.JSONDecodeError:
                                verification_result = {"output": payload.output}
                        conn.execute(
                            """
                            UPDATE data_sync_tasks
                            SET status = 'succeeded',
                                detail = detail || %s,
                                finished_at = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                Jsonb({
                                    "output": payload.output,
                                    "status": payload.status,
                                    "node_task_id": task_id,
                                    "verification_result": verification_result,
                                }),
                                ts,
                                ts,
                                task["data_sync_task_id"],
                            ),
                        )
                        if (
                            sync_task
                            and task["task_type"] == "verify_data_sync"
                            and sync_task["task_type"] == "shared_resource_sync"
                            and sync_task["resource_id"]
                        ):
                            conn.execute(
                                """
                                INSERT INTO node_cache_inventory (
                                    resource_id, node_id, path, status, size_bytes, file_count,
                                    verification, digest, last_error, last_synced_at,
                                    last_verified_at, updated_at
                                ) VALUES (%s, %s, %s, 'ready', %s, %s, %s, %s, '', %s, %s, %s)
                                ON CONFLICT (resource_id, node_id) DO UPDATE SET
                                    path = EXCLUDED.path, status = 'ready',
                                    size_bytes = EXCLUDED.size_bytes, file_count = EXCLUDED.file_count,
                                    verification = EXCLUDED.verification, digest = EXCLUDED.digest,
                                    last_error = '', last_synced_at = EXCLUDED.last_synced_at,
                                    last_verified_at = EXCLUDED.last_verified_at,
                                    updated_at = EXCLUDED.updated_at
                                """,
                                (
                                    sync_task["resource_id"], sync_task["target_node_id"], sync_task["target_path"],
                                    int(verification_result.get("size_bytes") or 0),
                                    int(verification_result.get("file_count") or 0),
                                    str(verification_result.get("method") or verification or "size"),
                                    str(verification_result.get("digest") or ""),
                                    ts, ts, ts,
                                ),
                            )
                    if sync_task and sync_task["task_type"] == "backup_user_home" and sync_task["user_id"]:
                        conn.execute(
                            "UPDATE user_data_policies SET last_backup_at = %s, updated_at = %s WHERE user_id = %s",
                            (ts, ts, sync_task["user_id"]),
                        )
                    if (
                        sync_task
                        and sync_task["task_type"] == "user_home_sync"
                        and sync_task["user_id"]
                        and sync_task["detail"].get("direction") == "container_to_storage"
                    ):
                        conn.execute("DELETE FROM user_directory_scans WHERE user_id = %s", (sync_task["user_id"],))
                    if (
                        sync_task
                        and sync_task["task_type"] == "shared_resource_upload"
                        and sync_task["resource_id"]
                        and task["task_type"] != "verify_data_sync"
                    ):
                        resource_id = sync_task["resource_id"]
                        resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
                        final_local_path = str(sync_task["detail"].get("final_local_path") or "")
                        final_platform_path = str(sync_task["detail"].get("final_platform_path") or "")
                        if resource and final_local_path and final_platform_path:
                            migrate_task = enqueue_node_task(
                                conn,
                                sync_task["target_node_id"],
                                None,
                                "migrate_shared_resource_path",
                                {
                                    "resource_id": resource_id,
                                    "resource_type": resource["resource_type"],
                                    "name": resource["name"],
                                    "version": resource["version"],
                                    "old_path": sync_task["target_path"],
                                    "new_path": final_local_path,
                                    "old_source_path": resource["source_path"],
                                    "new_source_path": final_platform_path,
                                    "create_symlink": False,
                                },
                            )
                            conn.execute(
                                "UPDATE shared_resources SET request_status = 'finalizing', updated_at = %s WHERE id = %s",
                                (ts, resource_id),
                            )
                            audit(
                                conn,
                                "system",
                                "finalize-upload",
                                f"shared-resource:{resource_id}",
                                {"node": sync_task["target_node_id"], "task_id": migrate_task["id"]},
                            )
                cleanup_container_sync_key(conn, task)
                audit(conn, "node-agent", "task-succeeded", f"node-task:{task_id}", {"type": task["task_type"]})
            else:
                error = payload.error.strip()[:4000]
                retry_scheduled = False
                conn.execute(
                    """
                    UPDATE node_tasks
                    SET status = 'failed', last_error = %s, result = %s, finished_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (error, Jsonb({"output": payload.output, "status": payload.status}), ts, ts, task_id),
                )
                if task["task_type"] == "incus_create_container" and task["container_id"]:
                    conn.execute(
                        "UPDATE containers SET status = 'failed', access_status = 'failed', access_error = %s, updated_at = %s WHERE id = %s",
                        (error, ts, task["container_id"]),
                    )
                if task["task_type"] == "incus_sync_ssh_keys" and task["container_id"]:
                    conn.execute(
                        "UPDATE containers SET access_status = 'failed', access_error = %s, updated_at = %s WHERE id = %s",
                        (error, ts, task["container_id"]),
                    )
                if task["task_type"] == "incus_sync_ports" and task["container_id"] and task_payload_has_ssh_port(task):
                    conn.execute(
                        "UPDATE containers SET access_status = 'failed', access_error = %s, updated_at = %s WHERE id = %s",
                        (error, ts, task["container_id"]),
                    )
                if task["task_type"] in (
                    "incus_start_container",
                    "incus_stop_container",
                    "incus_restart_container",
                    "incus_delete_container",
                ) and task["container_id"]:
                    conn.execute(
                        "UPDATE containers SET status = 'failed', updated_at = %s WHERE id = %s",
                        (ts, task["container_id"]),
                    )
                if task["task_type"] == "incus_image_export":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    storage_image_file_id = int(task_payload.get("storage_image_file_id") or 0)
                    if storage_image_file_id:
                        conn.execute(
                            """
                            UPDATE storage_image_files
                            SET status = 'failed', last_error = %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (error, ts, storage_image_file_id),
                        )
                if task["task_type"] == "incus_image_push_to_storage":
                    push_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    storage_image_file_id = int(push_payload.get("storage_image_file_id") or 0)
                    if storage_image_file_id:
                        conn.execute(
                            """
                            UPDATE storage_image_files
                            SET status = 'failed', last_error = %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (error, ts, storage_image_file_id),
                        )
                if task["task_type"] == "incus_publish_container":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    storage_image_file_id = int(task_payload.get("storage_image_file_id") or 0)
                    if storage_image_file_id:
                        conn.execute(
                            """
                            UPDATE storage_image_files
                            SET status = 'failed', last_error = %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (error, ts, storage_image_file_id),
                        )
                if task["task_type"] == "download_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        progress = {
                            "phase": "error",
                            "pct": 0,
                            "current_file": "",
                            "task_id": task_id,
                            "node": node["hostname"],
                        }
                        conn.execute(
	                            """
	                            UPDATE shared_resources
	                            SET request_status = 'failed',
	                                check_error = %s,
	                                download_progress = %s,
	                                updated_at = %s
	                            WHERE id = %s
                            """,
                            (error, Jsonb(progress), ts, resource_id),
                        )
                if task["task_type"] == "migrate_shared_resource_path":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    old_source_path = str(task_payload.get("old_source_path") or "")
                    new_source_path = str(task_payload.get("new_source_path") or "")
                    if resource_id:
                        progress = {
                            "phase": "migration_error",
                            "pct": 0,
                            "current_file": "",
                            "task_id": task_id,
                            "node": node["hostname"],
                            "from": old_source_path,
                            "to": new_source_path,
                        }
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET check_status = 'failed',
                                check_error = %s,
                                download_progress = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (error, Jsonb(progress), ts, resource_id),
                        )
                if task["task_type"] == "ensure_user_zfs_dataset":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    user_id = int(task_payload.get("user_id") or 0)
                    if user_id:
                        conn.execute(
                            """
                            UPDATE user_storage_datasets
                            SET status = 'failed', last_error = %s, updated_at = %s
                            WHERE user_id = %s
                            """,
                            (error, ts, user_id),
                        )
                if task["task_type"] == "remove_user_zfs_dataset":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    user_id = int(task_payload.get("user_id") or 0)
                    if user_id:
                        conn.execute(
                            """
                            UPDATE user_storage_datasets
                            SET status = 'failed', last_error = %s, updated_at = %s
                            WHERE user_id = %s
                            """,
                            (error, ts, user_id),
                        )
                if task["task_type"] == "remove_user_workspace_volume":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    user_id = int(task_payload.get("user_id") or 0)
                    node_id = int(task_payload.get("node_id") or 0)
                    if user_id and node_id:
                        conn.execute(
                            """
                            UPDATE user_workspace_volumes
                            SET status = 'failed', last_error = %s, updated_at = %s
                            WHERE user_id = %s AND node_id = %s
                            """,
                            (error, ts, user_id, node_id),
                        )
                if task["task_type"] == "verify_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        result_detail: dict[str, Any] = {}
                        if payload.output.strip():
                            try:
                                parsed = json.loads(payload.output)
                                if isinstance(parsed, dict):
                                    result_detail = parsed
                            except json.JSONDecodeError:
                                result_detail = {}
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET request_status = %s,
                                check_status = 'failed',
                                check_error = %s,
                                size_bytes = %s,
                                file_count = %s,
                                checked_at = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                "awaiting_manual_download" if task_payload.get("manual_finalize") else "failed",
                                error,
                                int(result_detail.get("size_bytes") or 0),
                                int(result_detail.get("file_count") or 0),
                                ts,
                                ts,
                                resource_id,
                            ),
                        )
                if task["task_type"] == "download_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        conn.execute("UPDATE shared_resources SET request_status='failed',check_status='failed',check_error=%s,updated_at=%s WHERE id=%s", (error, ts, resource_id))
                if task["task_type"] == "prepare_shared_resource_download":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        conn.execute(
                            "UPDATE shared_resources SET request_status='failed',check_status='failed',check_error=%s,updated_at=%s WHERE id=%s",
                            (error, ts, resource_id),
                        )
                if task["task_type"] == "sync_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        conn.execute(
                            """
                            UPDATE node_resource_cache
                            SET status = 'failed', error = %s, updated_at = %s
                            WHERE node_id = %s AND resource_id = %s
                            """,
                            (error[:2000], ts, task["node_id"], resource_id),
                        )
                if task["task_type"] == "scan_user_directory":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    user_id = int(task_payload.get("user_id") or 0)
                    relative_path = str(task_payload.get("relative_path") or "")
                    if user_id:
                        conn.execute(
                            """
                            UPDATE user_directory_scans
                            SET status = 'failed', error = %s, updated_at = %s
                            WHERE user_id = %s AND relative_path = %s
                            """,
                            (error, ts, user_id, relative_path),
                        )
                if task["task_type"] == "scan_shared_resource":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    relative_path = str(task_payload.get("relative_path") or "")
                    if resource_id:
                        conn.execute(
                            """
                            UPDATE shared_resource_scans
                            SET status = 'failed', error = %s, updated_at = %s
                            WHERE resource_id = %s AND relative_path = %s
                            """,
                            (error, ts, resource_id, relative_path),
                        )
                sync_task = None
                if task["data_sync_task_id"]:
                    sync_task = conn.execute(
                        "SELECT * FROM data_sync_tasks WHERE id = %s FOR UPDATE",
                        (task["data_sync_task_id"],),
                    ).fetchone()
                    detail = (sync_task or {}).get("detail", {})
                    retry_count = int(detail.get("retry_count") or 0)
                    max_retries = int(detail.get("max_retries") or 0)
                    retry_scheduled = bool(sync_task and retry_count < max_retries)
                    if retry_scheduled:
                        backoff = max(1, int(detail.get("retry_backoff_seconds") or 60))
                        delay = min(86400, backoff * (2**retry_count))
                        retry_task = enqueue_node_task(
                            conn,
                            task["node_id"],
                            task["container_id"],
                            task["task_type"],
                            task["payload"],
                            data_sync_task_id=task["data_sync_task_id"],
                            available_at=ts + delay,
                        )
                        conn.execute(
                            """
                            UPDATE data_sync_tasks
                            SET status = 'retrying', detail = detail || %s,
                                finished_at = 0, updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                Jsonb({
                                    "error": error,
                                    "retry_count": retry_count + 1,
                                    "retry_at": ts + delay,
                                    "retry_node_task_id": retry_task["id"],
                                    "failed_node_task_id": task_id,
                                }),
                                ts,
                                task["data_sync_task_id"],
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE data_sync_tasks
                            SET status = 'failed', detail = detail || %s,
                                finished_at = %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                Jsonb({
                                    "error": error,
                                    "output": payload.output,
                                    "status": payload.status,
                                    "node_task_id": task_id,
                                }),
                                ts,
                                ts,
                                task["data_sync_task_id"],
                            ),
                        )
                        if sync_task and sync_task["task_type"] == "shared_resource_sync" and sync_task["resource_id"]:
                            conn.execute(
                                """
                                INSERT INTO node_cache_inventory (
                                    resource_id, node_id, path, status, last_error, updated_at
                                ) VALUES (%s, %s, %s, 'failed', %s, %s)
                                ON CONFLICT (resource_id, node_id) DO UPDATE SET
                                    status = 'failed', last_error = EXCLUDED.last_error,
                                    updated_at = EXCLUDED.updated_at
                                """,
                                (sync_task["resource_id"], sync_task["target_node_id"],
                                 sync_task["target_path"], error, ts),
                            )
                    # user_home_sync 失败属于非阻断性错误：家目录不存在时计算节点会自动创建空目录，
                # 容器仍可正常启动，用户事后手动同步即可。只有其他同步类型失败才取消创建任务。
                sync_task_type = (sync_task or {}).get("task_type", "")
                if task["container_id"] and not retry_scheduled and sync_task_type != "user_home_sync":
                        conn.execute(
                            "UPDATE containers SET status = 'failed', updated_at = %s WHERE id = %s AND status = 'provisioning'",
                            (ts, task["container_id"]),
                        )
                        conn.execute(
                            """
                            UPDATE node_tasks
                            SET status = 'failed',
                                last_error = %s,
                                finished_at = %s,
                                updated_at = %s
                            WHERE container_id = %s
                              AND task_type = 'incus_create_container'
                              AND status IN ('pending', 'claimed')
                            """,
                            (f"blocked by failed data sync task {task['data_sync_task_id']}: {error}", ts, ts, task["container_id"]),
                        )
                if not retry_scheduled:
                    cleanup_container_sync_key(conn, task)
                audit(
                    conn,
                    "node-agent",
                    "task-retrying" if retry_scheduled else "task-failed",
                    f"node-task:{task_id}",
                    {"type": task["task_type"], "error": error, "retry_scheduled": retry_scheduled},
                )
        # with db() 块结束后事务已提交，此时再唤醒等待方可保证其读到最新数据
        signal_node_task_done(task_id)
        return {"ok": True}
