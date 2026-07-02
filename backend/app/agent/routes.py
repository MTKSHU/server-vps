import asyncio
import json
import uuid
from typing import Any

import asyncssh
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from ..auth import authenticate_token, is_admin_user, websocket_token
from psycopg.types.json import Jsonb

from ..schemas import AgentTaskClaim, AgentTaskProgress, AgentTaskResult, NodeRegistration
from ..nodes.routes import _get_or_create_ssh_key
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

    @app.post("/api/nodes/register", status_code=201)
    def register_node(payload: NodeRegistration):
        with db() as conn:
            return upsert_node(conn, payload)

    @app.post("/api/nodes/{node_id}/heartbeat")
    def heartbeat(node_id: int, payload: NodeRegistration):
        with db() as conn:
            existing = conn.execute("SELECT hostname FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="节点不存在")
            payload.hostname = existing["hostname"]
            return upsert_node(conn, payload)

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
            ssh_host = node["ip"]
            ssh_user = node.get("ssh_user") or "root"
            ssh_port = int(node.get("ssh_port") or 22)
            ssh_key = _get_or_create_ssh_key()
            async with asyncssh.connect(
                ssh_host, port=ssh_port, username=ssh_user,
                client_keys=[ssh_key], known_hosts=None,
            ) as ssh_conn:
                async with ssh_conn.create_process(
                    encoding=None, request_pty=True,
                    term_type="xterm-256color", term_size=(cols, rows),
                ) as proc:
                    with db() as conn:
                        audit(conn, user["username"], "terminal-open", f"node:{node_id}", {"node": node["hostname"]})
                    await websocket.send_json({"type": "started"})

                    async def forward_ssh_output():
                        try:
                            while True:
                                chunk = await proc.stdout.read(4096)
                                if not chunk:
                                    break
                                await websocket.send_json({"type": "data", "data": chunk.decode("utf-8", errors="replace")})
                        except Exception:
                            pass
                        try:
                            await websocket.send_json({"type": "exit"})
                        except Exception:
                            pass

                    read_task = asyncio.create_task(forward_ssh_output())
                    try:
                        while True:
                            msg = await websocket.receive_json()
                            if msg.get("type") == "input":
                                proc.stdin.write(msg.get("data", "").encode("utf-8", errors="replace"))
                            elif msg.get("type") == "resize":
                                proc.change_terminal_size(msg.get("cols", cols), msg.get("rows", rows))
                    except (WebSocketDisconnect, Exception):
                        pass
                    finally:
                        read_task.cancel()
        except asyncssh.Error as e:
            try:
                await websocket.send_json({"type": "error", "error": f"SSH 连接失败：{e}"})
            except Exception:
                pass
        except WebSocketDisconnect:
            pass
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
                "SELECT data_sync_task_id FROM node_tasks WHERE id = %s AND node_id = %s",
                (task_id, node["id"]),
            ).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="任务不存在")
            if task["data_sync_task_id"]:
                # 仅在任务尚未结束（planned/running）时更新，避免覆盖已完成状态
                conn.execute(
                    """
                    UPDATE data_sync_tasks
                    SET progress = %s, updated_at = %s
                    WHERE id = %s AND status IN ('planned', 'running')
                    """,
                    (Jsonb(payload.progress or {}), now_ts(), task["data_sync_task_id"]),
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
                    conn.execute("UPDATE node_tasks SET container_id = NULL WHERE id = %s", (task_id,))
                    conn.execute("DELETE FROM containers WHERE id = %s", (task["container_id"],))
                if task["task_type"] == "incus_image_export":
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    storage_image_file_id = int(task_payload.get("storage_image_file_id") or 0)
                    result_detail: dict[str, Any] = {}
                    if payload.output.strip():
                        try:
                            parsed = json.loads(payload.output)
                            if isinstance(parsed, dict):
                                result_detail = parsed
                        except json.JSONDecodeError:
                            result_detail = {}
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
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET check_status = 'ok',
                                size_bytes = %s,
                                file_count = %s,
                                check_error = '',
                                checked_at = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                int(result_detail.get("size_bytes") or 0),
                                int(result_detail.get("file_count") or 0),
                                ts,
                                ts,
                                resource_id,
                            ),
                        )
                if task["task_type"] in ("download_shared_resource", "download_huggingface_resource"):
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        conn.execute("UPDATE shared_resources SET request_status='ready',check_status='unknown',check_error='',updated_at=%s WHERE id=%s", (ts, resource_id))
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
                        conn.execute(
                            """
                            UPDATE shared_resources
                            SET check_status = 'failed', check_error = %s, updated_at = %s
                            WHERE id = %s
                            """,
                            (error, ts, resource_id),
                        )
                if task["task_type"] in ("download_shared_resource", "download_huggingface_resource"):
                    task_payload = task["payload"] if isinstance(task["payload"], dict) else {}
                    resource_id = int(task_payload.get("resource_id") or 0)
                    if resource_id:
                        conn.execute("UPDATE shared_resources SET request_status='failed',check_status='failed',check_error=%s,updated_at=%s WHERE id=%s", (error, ts, resource_id))
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
