import asyncio
import posixpath
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..config import SYNC_SSH_IDENTITY_FILE, SYNC_SSH_PORT, SYNC_SSH_USER, CONTAINER_ROOT_DISK_GB
from ..nodes.routes import generate_ephemeral_sync_keypair
from ..nodes.services import allowed_node_ids_for_user
from ..platform_settings import get_platform_settings
from ..agent.tasks import get_node_task_event, release_node_task_event, signal_node_task_done  # noqa: F401 (signal_node_task_done re-exported for agent/routes)
from ..schemas import (
    ContainerCreate,
    ContainerDeleteRequest,
    ContainerExecCreate,
    ContainerPortInput,
    ContainerPublishImageInput,
    ContainerResourceUpdate,
    ContainerSyncInput,
    ContainerSyncRuleInput,
)
from ..auth import is_admin_user, require_admin


def register_container_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]
    audit = deps["audit"]
    current_user = deps["current_user"]
    usage_for_user = deps["usage_for_user"]
    list_containers = deps["list_containers"]
    list_container_ports = deps["list_container_ports"]
    normalize_port_payload = deps["normalize_port_payload"]
    add_container_port = deps["add_container_port"]
    select_node_and_gpus = deps["select_node_and_gpus"]
    build_data_mounts = deps["build_data_mounts"]
    enqueue_incus_image_import_task = deps["enqueue_incus_image_import_task"]
    enqueue_node_task = deps["enqueue_node_task"]
    public_task = deps["public_task"]
    incus_create_payload = deps["incus_create_payload"]
    incus_lifecycle_payload = deps["incus_lifecycle_payload"]
    incus_ports_payload = deps["incus_ports_payload"]
    select_storage_node_for_path = deps["select_storage_node_for_path"]
    storage_root_for_node = deps["storage_root_for_node"]
    storage_image_base_name = deps["storage_image_base_name"]

    def require_container_access(user, container):
        if not is_admin_user(user) and container["owner_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="只能操作自己的容器")

    def validate_container_path(path: str) -> str:
        value = posixpath.normpath(path.strip())
        if not value.startswith("/") or value in ("/", "/.") or "\x00" in value:
            raise HTTPException(status_code=400, detail="容器路径必须是绝对路径，且不能是根目录")
        if "/../" in value or value.endswith("/.."):
            raise HTTPException(status_code=400, detail="容器路径不合法")
        if len(value) > 240:
            raise HTTPException(status_code=400, detail="容器路径过长")
        return value

    def normalize_storage_relative_path(value: str) -> str:
        value = value.replace("\\", "/").strip().strip("/")
        if not value:
            return ""
        parts = value.split("/")
        if any(part in ("", ".", "..") or "\x00" in part for part in parts):
            raise HTTPException(status_code=400, detail="存储相对路径不合法")
        if len(value) > 512:
            raise HTTPException(status_code=400, detail="存储相对路径过长")
        return "/".join(parts)

    def source_path_for_node(platform_path: str, node: dict[str, Any]) -> str:
        path = posixpath.normpath(platform_path.strip())
        storage_root = posixpath.normpath(str(node.get("storage_root") or "/data").strip() or "/data")
        if path == storage_root or path.startswith(storage_root.rstrip("/") + "/"):
            return path
        if path == "/data":
            return storage_root
        if path.startswith("/data/"):
            return posixpath.join(storage_root, path[len("/data/"):])
        return path

    def sync_endpoint(node: dict[str, Any]) -> dict[str, Any]:
        sync_ip = str(node.get("sync_ip") or "").strip()
        sync_port = int(node.get("sync_ssh_port") or 0)
        return {
            "hostname": node["hostname"],
            "host": sync_ip or node["ip"],
            "port": sync_port or int(node.get("ssh_port") or SYNC_SSH_PORT),
            "user": (node.get("ssh_user") or SYNC_SSH_USER),
            "identity_file": SYNC_SSH_IDENTITY_FILE,
        }

    def public_sync_task(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "id": row["id"],
            "task_type": row["task_type"],
            "user_id": row["user_id"],
            "resource_id": row["resource_id"],
            "target_node_id": row["target_node_id"],
            "container_id": row["container_id"],
            "source_path": row["source_path"],
            "target_path": row["target_path"],
            "status": row["status"],
            "detail": row["detail"],
            "progress": row.get("progress") or {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "last_error": row.get("last_error", ""),
            "result": row.get("result", {}),
        }

    def public_sync_rule(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "id": row["id"],
            "container_id": row["container_id"],
            "rule_type": row["rule_type"],
            "direction": row["direction"],
            "name": row["name"],
            "container_path": row["container_path"],
            "storage_relative_path": row["storage_relative_path"],
            "resource_id": row["resource_id"],
            "interval_minutes": row["interval_minutes"],
            "schedule_kind": row.get("schedule_kind", "daily"),
            "schedule_time_seconds": row.get("schedule_time_seconds", 0),
            "conflict_policy": row.get("conflict_policy") or "overwrite",
            "enabled": row["enabled"],
            "last_run_at": row["last_run_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def has_ssh_port(ports: list[dict[str, Any]]) -> bool:
        return any(str(port.get("protocol") or "").lower() == "tcp" and int(port.get("container_port") or 0) == 22 for port in ports)

    def user_home_path(conn, user_id: int) -> tuple[dict[str, Any], str]:
        row = conn.execute(
            """
            SELECT udp.*, u.username
            FROM user_data_policies udp
            JOIN users u ON u.id = udp.user_id
            WHERE udp.user_id = %s
            """,
            (user_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="用户目录策略不存在，请先打开存储中心初始化我的文件")
        return row, row["home_path"]

    def storage_path_for_sync(conn, user: dict[str, Any], payload: ContainerSyncInput | ContainerSyncRuleInput, storage_type: str, resource_id: int | None = None) -> tuple[str, int | None, dict[str, Any]]:
        relative_path = normalize_storage_relative_path(payload.storage_relative_path)
        if storage_type == "user_file":
            _, home_path = user_home_path(conn, user["id"])
            platform_path = f"{home_path.rstrip('/')}/{relative_path}" if relative_path else home_path
            node = select_storage_node_for_path(conn, platform_path)
            if not node:
                raise HTTPException(status_code=400, detail="没有 online 的 storage/mixed 节点可访问我的文件")
            return source_path_for_node(platform_path, node), None, node
        resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s AND enabled = TRUE", (resource_id or 0,)).fetchone()
        if not resource:
            raise HTTPException(status_code=404, detail="公开数据集/模型不存在")
        platform_path = f"{resource['source_path'].rstrip('/')}/{relative_path}" if relative_path else resource["source_path"]
        node = select_storage_node_for_path(conn, platform_path)
        if not node:
            raise HTTPException(status_code=400, detail="没有 online 的 storage/mixed 节点可访问公开资源")
        return source_path_for_node(platform_path, node), resource["id"], node

    def enqueue_container_sync(
        conn,
        user: dict[str, Any],
        container: dict[str, Any],
        *,
        direction: str,
        storage_type: str,
        resource_id: int | None,
        storage_relative_path: str,
        container_path: str,
        conflict_policy: str = "overwrite",
        rule_id: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        container_path = validate_container_path(container_path)
        payload_like = ContainerSyncInput(
            direction=direction,
            storage_type=storage_type,  # type: ignore[arg-type]
            resource_id=resource_id,
            storage_relative_path=storage_relative_path,
            container_path=container_path,
            conflict_policy=conflict_policy,  # type: ignore[arg-type]
        )
        storage_path, resolved_resource_id, storage_node = storage_path_for_sync(conn, user, payload_like, storage_type, resource_id)
        ts = now_ts()
        if direction == "storage_to_container":
            source_path, target_path = storage_path, container_path
        elif direction == "container_to_storage":
            source_path, target_path = container_path, storage_path
        else:
            raise HTTPException(status_code=400, detail="同步方向不合法")
        platform_settings = get_platform_settings(conn)
        bandwidth_limit_mbps = int(platform_settings.get("transfer_bandwidth_limit_mbps") or 0)
        detail = {
            "direction": direction,
            "storage_type": storage_type,
            "storage_relative_path": normalize_storage_relative_path(storage_relative_path),
            "conflict_policy": conflict_policy,
            "verification": "",
            "rule_id": rule_id or 0,
        }
        sync_task = conn.execute(
            """
            INSERT INTO data_sync_tasks (
                task_type, user_id, resource_id, source_node_id, target_node_id, container_id,
                source_path, target_path, status, detail, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'planned', %s, %s, %s)
            RETURNING *
            """,
            (
                "shared_resource_sync" if resolved_resource_id else "user_home_sync",
                user["id"],
                resolved_resource_id,
                storage_node["id"] if direction == "storage_to_container" else container["node_id"],
                container["node_id"] if direction == "storage_to_container" else storage_node["id"],
                container["id"],
                source_path,
                target_path,
                Jsonb(detail),
                ts,
                ts,
            ),
        ).fetchone()

        source_endpoint: dict[str, Any] = {}
        target_endpoint: dict[str, Any] = {}
        sync_key_id = ""
        sync_pubkey = ""
        cross_node = storage_node["id"] != container["node_id"]
        if cross_node:
            privkey, pubkey = generate_ephemeral_sync_keypair()
            sync_key_id = f"sync-{sync_task['id']}"
            sync_pubkey = pubkey
            install_task = enqueue_node_task(
                conn,
                storage_node["id"],
                None,
                "install_sync_pubkey",
                {
                    "public_key": pubkey,
                    "allowed_path": storage_path,
                    "key_id": sync_key_id,
                    "expires_at": ts + 7 * 86400,
                },
            )
            # install 任务写入后必须立即提交，否则 agent 在另一个连接中看不到该任务，
            # _wait_for_node_task 会一直等到超时，最终导致本次同步事务回滚、任务记录丢失。
            conn.commit()
            install_status, install_error = _wait_for_node_task(conn, install_task["id"])
            if install_status != "succeeded":
                conn.execute(
                    """
                    UPDATE data_sync_tasks
                    SET status = 'failed', detail = detail || %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        Jsonb({"install_error": install_error, "install_node_task_id": install_task["id"]}),
                        ts,
                        sync_task["id"],
                    ),
                )
                conn.commit()
                raise HTTPException(
                    status_code=500,
                    detail=f"存储节点临时同步密钥安装失败: {install_error or install_status}",
                )
            endpoint = {
                "hostname": storage_node["hostname"],
                "host": str(storage_node.get("sync_ip") or "").strip() or storage_node["ip"],
                "port": int(storage_node.get("sync_ssh_port") or 0) or int(storage_node.get("ssh_port") or SYNC_SSH_PORT),
                "user": storage_node.get("ssh_user") or SYNC_SSH_USER,
                "private_key": privkey,
                "restricted": True,
                "allowed_path": storage_path,
            }
            if direction == "storage_to_container":
                source_endpoint = endpoint
            else:
                target_endpoint = endpoint
            # 兜底清理：7 天后若结果回调未触发清理，则自动移除
            enqueue_node_task(
                conn,
                storage_node["id"],
                None,
                "remove_sync_pubkey",
                {"key_id": sync_key_id, "public_key": pubkey},
                available_at=ts + 7 * 86400 + 300,
            )

        task_payload: dict[str, Any] = {
            "sync_task_id": sync_task["id"],
            "source_node_id": storage_node["id"] if direction == "storage_to_container" else container["node_id"],
            "target_node_id": container["node_id"] if direction == "storage_to_container" else storage_node["id"],
            "container_name": container["name"],
            "source_path": source_path,
            "target_path": target_path,
            "mode": direction,
            "delete": conflict_policy == "overwrite",
            "ignore_existing": conflict_policy == "skip",
            "bandwidth_limit_mbps": bandwidth_limit_mbps,
            "source_endpoint": source_endpoint,
            "target_endpoint": target_endpoint,
            "sync_key_id": sync_key_id,
            "sync_pubkey": sync_pubkey,
            "sync_storage_node_id": storage_node["id"] if cross_node else 0,
        }
        node_task = enqueue_node_task(
            conn,
            container["node_id"],
            container["id"],
            "container_data_sync",
            task_payload,
            data_sync_task_id=sync_task["id"],
        )
        return sync_task, node_task

    def _wait_for_node_task(conn, task_id: int, timeout: float = 15, interval: float = 0.3) -> tuple[str, str]:
        # 先注册 Event，再做初次检查，避免「检查后完成、Event 未注册」的竞态
        event = get_node_task_event(task_id)
        try:
            row = conn.execute("SELECT status, last_error FROM node_tasks WHERE id = %s", (task_id,)).fetchone()
            if row and row["status"] in ("succeeded", "failed"):
                return row["status"], row["last_error"] or ""
            # 等待 complete_node_task 触发唤醒（最多 timeout 秒，替代 DB 轮询）
            event.wait(timeout=timeout)
            row = conn.execute("SELECT status, last_error FROM node_tasks WHERE id = %s", (task_id,)).fetchone()
            if row and row["status"] in ("succeeded", "failed"):
                return row["status"], row["last_error"] or ""
            return "timeout", "等待任务结果超时"
        finally:
            release_node_task_event(task_id)

    def schedule_rule_due(row: dict[str, Any], ts: int, timezone_name: str) -> bool:
        try:
            tz = ZoneInfo(timezone_name or "Asia/Shanghai")
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("Asia/Shanghai")
        local_now = datetime.fromtimestamp(ts, tz)
        schedule_time = int(row.get("schedule_time_seconds") or 0)
        seconds_today = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
        if seconds_today < schedule_time:
            return False
        last_run_at = int(row.get("last_run_at") or 0)
        if last_run_at <= 0:
            return True
        kind = row.get("schedule_kind") or "daily"
        last_local = datetime.fromtimestamp(last_run_at, tz)
        if kind == "daily":
            return local_now.date() > last_local.date()
        return last_run_at + int(row.get("interval_minutes") or 1) * 60 <= ts

    async def scheduled_container_sync_loop():
        while True:
            await asyncio.sleep(60)
            try:
                with db() as conn:
                    ts = now_ts()
                    settings = get_platform_settings(conn)
                    rows = conn.execute(
                        """
                        SELECT csr.*, c.name AS container_name, c.owner_id, c.node_id, c.status,
                               u.username, u.role, u.group_name
                        FROM container_sync_rules csr
                        JOIN containers c ON c.id = csr.container_id
                        JOIN users u ON u.id = c.owner_id
                        WHERE csr.enabled = TRUE
                          AND csr.rule_type = 'scheduled_upload'
                          AND csr.direction = 'container_to_storage'
                          AND c.status IN ('running', 'stopped')
                        ORDER BY csr.last_run_at, csr.id
                        LIMIT 20
                        """,
                    ).fetchall()
                    for row in rows:
                        if not schedule_rule_due(row, ts, settings["platform_timezone"]):
                            continue
                        container = {
                            "id": row["container_id"],
                            "name": row["container_name"],
                            "owner_id": row["owner_id"],
                            "node_id": row["node_id"],
                            "status": row["status"],
                        }
                        user = {"id": row["owner_id"], "username": row["username"], "role": row["role"], "group_name": row["group_name"]}
                        try:
                            await asyncio.to_thread(
                                enqueue_container_sync,
                                conn,
                                user,
                                container,
                                direction="container_to_storage",
                                storage_type="user_file",
                                resource_id=None,
                                storage_relative_path=row["storage_relative_path"],
                                container_path=row["container_path"],
                                conflict_policy=row.get("conflict_policy") or "overwrite",
                                rule_id=row["id"],
                            )
                            conn.execute(
                                "UPDATE container_sync_rules SET last_run_at = %s, updated_at = %s WHERE id = %s",
                                (ts, ts, row["id"]),
                            )
                        except Exception as exc:
                            conn.execute(
                                "UPDATE container_sync_rules SET updated_at = %s WHERE id = %s",
                                (ts, row["id"]),
                            )
                            audit(conn, "system", "scheduled-sync-failed", f"container-sync-rule:{row['id']}", {"error": str(exc)[:500]})
            except Exception as exc:
                # 记录调度器本身的意外异常，避免静默失败导致定时同步停止工作
                import sys
                print(f"[WARN] scheduled_container_sync_loop 异常: {exc!r}", file=sys.stderr, flush=True)

    @app.on_event("startup")
    async def start_container_sync_scheduler():
        asyncio.create_task(scheduled_container_sync_loop())
        asyncio.create_task(container_expiry_loop())

    async def container_expiry_loop():
        """每 5 分钟检查一次到期容器，停止运行中的、删除已停止且超期 1 天以上的。"""
        while True:
            await asyncio.sleep(300)
            try:
                with db() as conn:
                    ts = now_ts()
                    expired = conn.execute(
                        """
                        SELECT * FROM containers
                        WHERE expires_at > 0 AND expires_at <= %s
                          AND status IN ('running', 'stopped', 'provisioning')
                        """,
                        (ts,),
                    ).fetchall()
                    for container in expired:
                        try:
                            if container["status"] == "running":
                                conn.execute(
                                    "UPDATE containers SET status='stopping', updated_at=%s WHERE id=%s",
                                    (ts, container["id"]),
                                )
                                enqueue_node_task(
                                    conn,
                                    container["node_id"],
                                    container["id"],
                                    "incus_stop_container",
                                    {"container_id": container["id"], "name": container["name"], "operation": "stop", "previous_status": "running"},
                                )
                                audit(conn, "system", "auto-stop-expired", f"container:{container['id']}", {"expires_at": container["expires_at"]})
                            elif container["status"] in ("stopped",) and container["expires_at"] <= ts - 86400:
                                # 停止超过 1 天且已到期 → 自动删除容器记录
                                conn.execute(
                                    "UPDATE containers SET status='deleting', updated_at=%s WHERE id=%s",
                                    (ts, container["id"]),
                                )
                                enqueue_node_task(
                                    conn,
                                    container["node_id"],
                                    container["id"],
                                    "incus_delete_container",
                                    {"container_id": container["id"], "name": container["name"], "force": True},
                                )
                                audit(conn, "system", "auto-delete-expired", f"container:{container['id']}", {"expires_at": container["expires_at"]})
                        except Exception as exc:
                            import sys
                            print(f"[WARN] container_expiry: container {container['id']} error: {exc!r}", file=sys.stderr, flush=True)
            except Exception as exc:
                import sys
                print(f"[WARN] container_expiry_loop: {exc!r}", file=sys.stderr, flush=True)

    from ..schemas import ContainerOut, DataSyncTaskOut

    @app.get("/api/containers", response_model=list[ContainerOut])
    def containers():
        with db() as conn:
            user = current_user(conn)
            rows = list_containers(conn)
            if is_admin_user(user):
                return rows
            # 普通用户只返回自己的容器，不隐藏 node_id（前端需要此字段）
            return [row for row in rows if row["owner_id"] == user["id"]]

    @app.post("/api/containers", status_code=201)
    def create_container(payload: ContainerCreate):
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", payload.name):
            raise HTTPException(status_code=400, detail="容器名称必须以小写字母开头，只包含小写字母、数字和连字符")
        payload.ssh_username = payload.ssh_username.strip() or "ubuntu"
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", payload.ssh_username):
            raise HTTPException(status_code=400, detail="初始用户名不合法")
        with db() as conn:
            user = current_user(conn)
            if payload.gpu_ids:
                payload.gpu_ids = list(dict.fromkeys(gpu_id for gpu_id in payload.gpu_ids if gpu_id > 0))
                payload.gpu_count = len(payload.gpu_ids)
            # 优先从 user_ssh_keys 表收集所有有效公钥（expires_at=0 永久有效）
            ts_now = now_ts()
            valid_keys = conn.execute(
                "SELECT public_key FROM user_ssh_keys WHERE user_id=%s AND (expires_at=0 OR expires_at>%s) ORDER BY created_at",
                (user["id"], ts_now),
            ).fetchall()
            if valid_keys:
                payload.ssh_key = payload.ssh_key.strip() or "\n".join(r["public_key"] for r in valid_keys)
            else:
                payload.ssh_key = payload.ssh_key.strip() or user["ssh_key"].strip()
            if not payload.ssh_key:
                raise HTTPException(status_code=400, detail="请填写 SSH 公钥，平台已禁用容器 SSH 密码登录")
            quota = conn.execute("SELECT * FROM quotas WHERE user_id = %s", (user["id"],)).fetchone()
            # workspace 数据卷大小：min(节点 max_disk_gb_per_container, 用户 container_disk_limit_gb)；如均未限制取节点可用磁盘
            quota_data_vol_gb = int(quota.get("container_disk_limit_gb") or 0)
            usage = usage_for_user(conn, user["id"])
            for key, requested in [
                ("cpu_cores", payload.cpu_cores),
                ("memory_gb", payload.memory_gb),
                ("gpu_count", payload.gpu_count),
                ("container_count", 1),
            ]:
                if usage[key] + requested > quota[key]:
                    raise HTTPException(status_code=400, detail=f"超出用户额度：{key}")
            image = conn.execute("SELECT * FROM images WHERE id = %s AND enabled = TRUE", (payload.image_id,)).fetchone()
            if not image:
                raise HTTPException(status_code=400, detail="镜像不存在")
            allowed_node_ids = allowed_node_ids_for_user(conn, user)
            node, selected_gpus, schedule_reasons = select_node_and_gpus(conn, image, payload, allowed_node_ids)
            if not node:
                detail = "没有满足资源、GPU 型号或镜像兼容性的节点"
                if schedule_reasons:
                    detail += "：" + "；".join(schedule_reasons[:5])
                raise HTTPException(status_code=400, detail=detail)
            # 固定 root disk 大小；workspace 卷大小由配额和节点限制决定
            root_disk_gb = CONTAINER_ROOT_DISK_GB
            node_disk_limit = int(node.get("max_disk_gb_per_container") or 0)
            node_disk_avail = max(0, int(node.get("disk_total_gb") or 0) - int(node.get("reserved_disk_gb") or 0) - int(node.get("disk_used_gb") or 0))
            # workspace_gb = min(节点上限, 用户配额上限)；如均为 0 则取节点可用磁盘
            if node_disk_limit > 0 and quota_data_vol_gb > 0:
                workspace_gb = min(node_disk_limit, quota_data_vol_gb)
            elif node_disk_limit > 0:
                workspace_gb = node_disk_limit
            elif quota_data_vol_gb > 0:
                workspace_gb = min(quota_data_vol_gb, node_disk_avail) if node_disk_avail > 0 else quota_data_vol_gb
            else:
                workspace_gb = node_disk_avail
            workspace_volume_name = f"user-{user['id']}-ws"
            ts = now_ts()
            conn.execute(
                """
                INSERT INTO user_workspace_volumes (
                    user_id, node_id, volume_name, quota_gb, status, last_error, created_at, updated_at, removed_at
                ) VALUES (%s, %s, %s, %s, 'active', '', %s, %s, 0)
                ON CONFLICT (user_id, node_id) DO UPDATE SET
                    volume_name = EXCLUDED.volume_name,
                    quota_gb = EXCLUDED.quota_gb,
                    status = 'active',
                    last_error = '',
                    updated_at = EXCLUDED.updated_at,
                    removed_at = 0
                """,
                (user["id"], node["id"], workspace_volume_name, workspace_gb, ts, ts),
            )
            generated_mounts = build_data_mounts(
                conn,
                user,
                payload.ssh_username,
                node["id"],
                payload.resources,
            )
            mounts = payload.mounts or generated_mounts
            container = conn.execute(
                """
                INSERT INTO containers (
                    name, owner_id, node_id, image_id, status, cpu_cores, memory_gb, disk_gb,
                    ssh_username, ssh_key, mounts, ip, access_status, access_error, expires_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'provisioning', %s, %s, %s, %s, %s, %s, %s, 'pending', '', %s, %s, %s)
                RETURNING *
                """,
                (
                    payload.name,
                    user["id"],
                    node["id"],
                    image["id"],
                    payload.cpu_cores,
                    payload.memory_gb,
                    root_disk_gb,
                    payload.ssh_username,
                    payload.ssh_key,
                    Jsonb(mounts),
                    f"10.99.{node['id']}.{100 + (ts % 100)}",
                    payload.expires_at or 0,
                    ts,
                    ts,
                ),
            ).fetchone()
            for selected in payload.resources:
                resource = conn.execute("SELECT * FROM shared_resources WHERE id=%s AND enabled=TRUE", (selected.resource_id,)).fetchone()
                if not resource:
                    raise HTTPException(status_code=400, detail=f"公开资源 {selected.resource_id} 不存在")
                mount_path = selected.mount_path.strip() or resource["mount_path"]
                conn.execute("INSERT INTO container_resources VALUES (%s,%s,%s,%s)", (container["id"], resource["id"], mount_path, ts))
            for gpu in selected_gpus:
                conn.execute("INSERT INTO container_gpus VALUES (%s, %s)", (container["id"], gpu["id"]))
            created_ports = []
            for port in payload.ports:
                created_ports.append(add_container_port(conn, container["id"], port))
            image_ref = image["incus_ref"] or image["id"]
            import_task = enqueue_incus_image_import_task(conn, node, container["id"], image_ref)
            enqueue_node_task(
                conn,
                node["id"],
                container["id"],
                "incus_create_container",
                incus_create_payload(container, image, node, selected_gpus, created_ports,
                                     workspace_volume_name=workspace_volume_name,
                                     workspace_volume_gb=workspace_gb),
            )
            audit(
                conn,
                user["username"],
                "create",
                f"container:{container['id']}",
                {
                    "name": payload.name,
                    "node": node["hostname"],
                    "status": "provisioning",
                    "image_import_task_id": import_task["id"] if import_task else 0,
                },
            )
            return next(item for item in list_containers(conn) if item["id"] == container["id"])

    def enqueue_container_lifecycle(container_id: int, operation: str, pending_status: str):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] in ("provisioning", "starting", "stopping", "restarting", "deleting"):
                raise HTTPException(status_code=409, detail=f"容器正在 {container['status']}，请稍后再试")
            ts = now_ts()
            conn.execute("UPDATE containers SET status = %s, updated_at = %s WHERE id = %s", (pending_status, ts, container_id))
            enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                f"incus_{operation}_container",
                incus_lifecycle_payload(container, operation),
            )
            audit(conn, user["username"], operation, f"container:{container_id}", {"name": container["name"]})
            return next(item for item in list_containers(conn) if item["id"] == container_id)

    @app.post("/api/containers/{container_id}/start", response_model=ContainerOut)
    def start_container(container_id: int):
        return enqueue_container_lifecycle(container_id, "start", "starting")

    @app.post("/api/containers/{container_id}/stop", response_model=ContainerOut)
    def stop_container(container_id: int):
        return enqueue_container_lifecycle(container_id, "stop", "stopping")

    @app.post("/api/containers/{container_id}/restart", response_model=ContainerOut)
    def restart_container(container_id: int):
        return enqueue_container_lifecycle(container_id, "restart", "restarting")

    @app.post("/api/containers/{container_id}/retry", status_code=202)
    def retry_container(container_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] != "failed":
                raise HTTPException(status_code=409, detail="只有 failed 状态的容器可以重试创建")
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (container["node_id"],)).fetchone()
            if not node:
                raise HTTPException(status_code=400, detail="容器所在节点不存在")
            image = conn.execute("SELECT * FROM images WHERE id = %s", (container["image_id"],)).fetchone()
            if not image:
                raise HTTPException(status_code=400, detail="容器镜像不存在")
            selected_gpus = conn.execute(
                "SELECT g.* FROM container_gpus cg JOIN gpus g ON g.id = cg.gpu_id WHERE cg.container_id = %s",
                (container_id,),
            ).fetchall()
            ports = list_container_ports(conn, container_id)
            ts = now_ts()
            conn.execute(
                "UPDATE containers SET status = 'provisioning', access_status = 'pending', access_error = '', updated_at = %s WHERE id = %s",
                (ts, container_id),
            )
            image_ref = image["incus_ref"] or image["id"]
            enqueue_incus_image_import_task(conn, node, container_id, image_ref)
            enqueue_node_task(
                conn,
                node["id"],
                container_id,
                "incus_create_container",
                incus_create_payload(container, image, node, selected_gpus, ports),
            )
            audit(conn, user["username"], "retry", f"container:{container_id}", {"name": container["name"]})
            return next(item for item in list_containers(conn) if item["id"] == container_id)

    @app.post("/api/containers/{container_id}/ssh-access/retry", status_code=202)
    def retry_container_ssh_access(container_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] != "running":
                raise HTTPException(status_code=409, detail="只有 running 状态的容器可以重试 SSH 准备")
            if not str(container.get("ssh_key") or "").strip():
                raise HTTPException(status_code=400, detail="容器缺少 SSH 公钥，无法准备 SSH")
            ts = now_ts()
            conn.execute(
                "UPDATE containers SET access_status = 'pending', access_error = '', updated_at = %s WHERE id = %s",
                (ts, container_id),
            )
            task = enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                "incus_sync_ssh_keys",
                {
                    "container_id": container_id,
                    "name": container["name"],
                    "ssh_username": container["ssh_username"] or "ubuntu",
                    "ssh_key": container["ssh_key"],
                    "mounts": container["mounts"] or [],
                },
            )
            audit(conn, user["username"], "retry-ssh-access", f"container:{container_id}", {"task_id": task["id"]})
            return {"task": public_task(task), "container": next(item for item in list_containers(conn) if item["id"] == container_id)}

    @app.patch("/api/containers/{container_id}/resources", status_code=202)
    def update_container_resources(container_id: int, payload: ContainerResourceUpdate):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] not in ("running", "stopped"):
                raise HTTPException(status_code=409, detail="只有 running 或 stopped 状态的容器可以修改配置")
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (container["node_id"],)).fetchone()
            if not node:
                raise HTTPException(status_code=400, detail="节点不存在")
            # 验证资源限额（与创建时相同规则）
            quota = conn.execute("SELECT * FROM quotas WHERE user_id = %s", (user["id"],)).fetchone()
            usage = usage_for_user(conn, user["id"])
            max_cpu = int(node.get("max_cpu_per_container") or 0) or node["cpu_total"]
            max_memory = int(node.get("max_memory_gb_per_container") or 0) or node["memory_total_gb"]
            if payload.cpu_cores < 1:
                raise HTTPException(status_code=400, detail="CPU 核数不能小于 1")
            if payload.memory_gb < 1:
                raise HTTPException(status_code=400, detail="内存不能小于 1 GB")
            if payload.cpu_cores > max_cpu:
                raise HTTPException(status_code=400, detail=f"超出节点单容器 CPU 上限：{max_cpu} 核")
            if payload.memory_gb > max_memory:
                raise HTTPException(status_code=400, detail=f"超出节点单容器内存上限：{max_memory} GB")
            # 检查用户配额（排除当前容器自身的用量）
            cpu_others = usage["cpu_cores"] - container["cpu_cores"]
            memory_others = usage["memory_gb"] - container["memory_gb"]
            if cpu_others + payload.cpu_cores > quota["cpu_cores"]:
                raise HTTPException(status_code=400, detail="超出用户 CPU 配额")
            if memory_others + payload.memory_gb > quota["memory_gb"]:
                raise HTTPException(status_code=400, detail="超出用户内存配额")
            # 处理 GPU 变更
            old_gpus = conn.execute(
                "SELECT g.* FROM container_gpus cg JOIN gpus g ON g.id = cg.gpu_id WHERE cg.container_id = %s",
                (container_id,),
            ).fetchall()
            new_selected_gpus = old_gpus  # 默认保持不变
            if payload.gpu_count != len(old_gpus) or (payload.gpu_model and any(g["model"] != payload.gpu_model for g in old_gpus)):
                from ..containers.scheduler import schedulable_gpus_for_node
                available = schedulable_gpus_for_node(conn, node, payload.gpu_model)
                # 排除本容器已占用的 GPU（允许保留）
                container_gpu_ids = {g["id"] for g in old_gpus}
                available_extra = [g for g in available if g["id"] not in container_gpu_ids]
                if payload.gpu_count == 0:
                    new_selected_gpus = []
                elif payload.gpu_count <= len(old_gpus):
                    new_selected_gpus = old_gpus[:payload.gpu_count]
                else:
                    need_extra = payload.gpu_count - len(old_gpus)
                    if len(available_extra) < need_extra:
                        raise HTTPException(status_code=400, detail=f"节点上没有足够的可用 GPU（需要 {need_extra} 张额外 GPU）")
                    new_selected_gpus = list(old_gpus) + available_extra[:need_extra]
                gpu_others = usage["gpu_count"] - len(old_gpus)
                if gpu_others + payload.gpu_count > quota["gpu_count"]:
                    raise HTTPException(status_code=400, detail="超出用户 GPU 配额")
            ts = now_ts()
            conn.execute(
                "UPDATE containers SET cpu_cores=%s, memory_gb=%s, updated_at=%s WHERE id=%s",
                (payload.cpu_cores, payload.memory_gb, ts, container_id),
            )
            conn.execute("DELETE FROM container_gpus WHERE container_id=%s", (container_id,))
            for gpu in new_selected_gpus:
                conn.execute("INSERT INTO container_gpus VALUES (%s, %s)", (container_id, gpu["id"]))
            task = enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                "incus_config_update",
                {
                    "container_id": container_id,
                    "name": container["name"],
                    "cpu_cores": payload.cpu_cores,
                    "memory_gb": payload.memory_gb,
                    "gpus": [
                        {"slot": g["slot"], "uuid": g["uuid"], "model": g["model"], "pci_address": g["pci_address"]}
                        for g in new_selected_gpus
                    ],
                },
            )
            audit(conn, user["username"], "update-resources", f"container:{container_id}",
                  {"cpu_cores": payload.cpu_cores, "memory_gb": payload.memory_gb, "gpu_count": payload.gpu_count})
            return {"task_id": task["id"], "container": next(item for item in list_containers(conn) if item["id"] == container_id)}


    @app.post("/api/containers/{container_id}/exec", status_code=202)
    def create_container_exec(container_id: int, payload: ContainerExecCreate):
        command = payload.command.strip()
        if not command:
            raise HTTPException(status_code=400, detail="命令不能为空")
        if len(command) > 2000:
            raise HTTPException(status_code=400, detail="命令过长")
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] != "running":
                raise HTTPException(status_code=400, detail="只有 running 容器可以打开 shell")
            task = enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                "incus_exec_command",
                {
                    "container_id": container_id,
                    "name": container["name"],
                    "command": command,
                },
            )
            audit(conn, "admin", "exec", f"container:{container_id}", {"command": command[:120]})
            return public_task(task)

    @app.post("/api/containers/{container_id}/publish-image", status_code=202)
    def publish_container_image(container_id: int, payload: ContainerPublishImageInput):
        require_admin()
        alias = payload.alias.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,80}", alias):
            raise HTTPException(status_code=400, detail="镜像别名格式不合法")
        with db() as conn:
            actor = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id=%s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (container["node_id"],)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="容器节点不存在")
            task_payload = {"container_id": container_id, "name": container["name"], "alias": alias}
            storage_image_file = None
            if payload.export_to_storage:
                base_name = storage_image_base_name(alias, alias)
                root = storage_root_for_node(conn, node["id"])
                export_dir = f"{root}/incus-images/{base_name}"
                ts = now_ts()
                storage_image_file = conn.execute(
                    """
                    INSERT INTO storage_image_files (
                        source_node_id, owner_id, fingerprint, aliases, description, architecture,
                        export_dir, base_name, size_bytes, status, last_error, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, '', %s, %s, 0, 'pending', '', %s, %s)
                    ON CONFLICT (source_node_id, fingerprint) DO UPDATE SET
                        owner_id = EXCLUDED.owner_id,
                        aliases = EXCLUDED.aliases,
                        description = EXCLUDED.description,
                        export_dir = EXCLUDED.export_dir,
                        base_name = EXCLUDED.base_name,
                        status = 'pending',
                        last_error = '',
                        updated_at = EXCLUDED.updated_at
                    RETURNING *
                    """,
                    (
                        node["id"],
                        actor["id"],
                        alias,
                        alias,
                        payload.display_name.strip() or alias,
                        export_dir,
                        base_name,
                        ts,
                        ts,
                    ),
                ).fetchone()
                task_payload.update({"storage_image_file_id": storage_image_file["id"], "export_dir": export_dir, "base_name": base_name})
            if payload.register_platform:
                ts = now_ts()
                image_id = alias.replace(":", "-")
                conn.execute(
                    """
                    INSERT INTO images (id, name, cuda_major, compatible_pools, incus_ref, enabled, preferred, owner, created_at, updated_at)
                    VALUES (%s, %s, 0, 'legacy-pascal,modern-geforce,workstation,unknown', %s, TRUE, TRUE, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        incus_ref = EXCLUDED.incus_ref,
                        enabled = TRUE,
                        preferred = TRUE,
                        owner = EXCLUDED.owner,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (image_id, payload.display_name.strip() or alias, alias, actor["username"], ts, ts),
                )
            task = enqueue_node_task(conn, container["node_id"], container_id, "incus_publish_container", task_payload)
            audit(conn, actor["username"], "publish-image", f"container:{container_id}", {"alias": alias, "export": bool(storage_image_file)})
            return public_task(task)

    @app.get("/api/containers/{container_id}/exec/{task_id}")
    def get_container_exec(container_id: int, task_id: int):
        with db() as conn:
            user = current_user(conn)
            task = conn.execute(
                """
                SELECT * FROM node_tasks
                WHERE id = %s AND container_id = %s AND task_type = 'incus_exec_command'
                """,
                (task_id, container_id),
            ).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="命令任务不存在")
            container = conn.execute("SELECT * FROM containers WHERE id=%s", (container_id,)).fetchone()
            require_container_access(user, container)
            return public_task(task)

    @app.get("/api/containers/{container_id}/sync-tasks", response_model=list[DataSyncTaskOut])
    def container_sync_tasks(container_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            rows = conn.execute(
                """
                SELECT dst.*,
                       COALESCE(nt.last_error, '') AS last_error,
                       COALESCE(nt.result, '{}'::jsonb) AS result
                FROM data_sync_tasks dst
                LEFT JOIN node_tasks nt ON nt.data_sync_task_id = dst.id
                WHERE dst.container_id = %s
                ORDER BY dst.created_at DESC, dst.id DESC
                LIMIT 50
                """,
                (container_id,),
            ).fetchall()
            return [public_sync_task(row) for row in rows]

    @app.post("/api/containers/{container_id}/sync", status_code=202)
    def create_container_sync(container_id: int, payload: ContainerSyncInput):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] not in ("running", "stopped"):
                raise HTTPException(status_code=400, detail="容器必须是 running 或 stopped 状态才能同步")
            sync_task, node_task = enqueue_container_sync(
                conn,
                user,
                container,
                direction=payload.direction,
                storage_type=payload.storage_type,
                resource_id=payload.resource_id,
                storage_relative_path=payload.storage_relative_path,
                container_path=payload.container_path,
                conflict_policy=payload.conflict_policy,
            )
            audit(conn, user["username"], "sync", f"container:{container_id}", {"direction": payload.direction, "sync_task_id": sync_task["id"]})
            return {"sync_task": public_sync_task(sync_task), "node_task": public_task(node_task)}

    @app.get("/api/containers/{container_id}/sync-rules")
    def container_sync_rules(container_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            rows = conn.execute(
                """
                SELECT *
                FROM container_sync_rules
                WHERE container_id = %s
                ORDER BY enabled DESC, created_at DESC, id DESC
                """,
                (container_id,),
            ).fetchall()
            return [public_sync_rule(row) for row in rows]

    @app.post("/api/containers/{container_id}/sync-rules", status_code=201)
    def create_container_sync_rule(container_id: int, payload: ContainerSyncRuleInput):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            container_path = validate_container_path(payload.container_path)
            storage_relative_path = normalize_storage_relative_path(payload.storage_relative_path)
            interval = max(1, min(int(payload.interval_minutes), 43200))
            schedule_kind = payload.schedule_kind if payload.schedule_kind in ("daily", "weekly", "monthly") else "daily"
            schedule_time_seconds = max(0, min(int(payload.schedule_time_seconds), 86399))
            conflict_policy = payload.conflict_policy if payload.conflict_policy in ("overwrite", "skip") else "overwrite"
            ts = now_ts()
            row = conn.execute(
                """
                INSERT INTO container_sync_rules (
                    container_id, rule_type, direction, name, container_path, storage_relative_path,
                    resource_id, interval_minutes, schedule_kind, schedule_time_seconds,
                    conflict_policy, enabled, last_run_at, created_at, updated_at
                ) VALUES (%s, 'scheduled_upload', 'container_to_storage', %s, %s, %s, NULL, %s, %s, %s, %s, %s, 0, %s, %s)
                RETURNING *
                """,
                (
                    container_id,
                    payload.name.strip()[:120] or "定时上传",
                    container_path,
                    storage_relative_path,
                    interval,
                    schedule_kind,
                    schedule_time_seconds,
                    conflict_policy,
                    payload.enabled,
                    ts,
                    ts,
                ),
            ).fetchone()
            audit(conn, user["username"], "create", f"container-sync-rule:{row['id']}", {"container_id": container_id})
            return public_sync_rule(row)

    @app.put("/api/containers/{container_id}/sync-rules/{rule_id}")
    def update_container_sync_rule(container_id: int, rule_id: int, payload: ContainerSyncRuleInput):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            conflict_policy = payload.conflict_policy if payload.conflict_policy in ("overwrite", "skip") else "overwrite"
            row = conn.execute(
                """
                UPDATE container_sync_rules
                SET name = %s,
                    container_path = %s,
                    storage_relative_path = %s,
                    interval_minutes = %s,
                    schedule_kind = %s,
                    schedule_time_seconds = %s,
                    conflict_policy = %s,
                    enabled = %s,
                    updated_at = %s
                WHERE id = %s AND container_id = %s
                RETURNING *
                """,
                (
                    payload.name.strip()[:120] or "定时上传",
                    validate_container_path(payload.container_path),
                    normalize_storage_relative_path(payload.storage_relative_path),
                    max(1, min(int(payload.interval_minutes), 43200)),
                    payload.schedule_kind if payload.schedule_kind in ("daily", "weekly", "monthly") else "daily",
                    max(0, min(int(payload.schedule_time_seconds), 86399)),
                    conflict_policy,
                    payload.enabled,
                    now_ts(),
                    rule_id,
                    container_id,
                ),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="同步规则不存在")
            return public_sync_rule(row)

    @app.delete("/api/containers/{container_id}/sync-rules/{rule_id}", status_code=204)
    def delete_container_sync_rule(container_id: int, rule_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            deleted = conn.execute(
                "DELETE FROM container_sync_rules WHERE id = %s AND container_id = %s RETURNING id",
                (rule_id, container_id),
            ).fetchone()
            if not deleted:
                raise HTTPException(status_code=404, detail="同步规则不存在")

    @app.post("/api/containers/{container_id}/sync-rules/{rule_id}/run", status_code=202)
    def run_container_sync_rule(container_id: int, rule_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            rule = conn.execute(
                "SELECT * FROM container_sync_rules WHERE id = %s AND container_id = %s",
                (rule_id, container_id),
            ).fetchone()
            if not rule:
                raise HTTPException(status_code=404, detail="同步规则不存在")
            sync_task, node_task = enqueue_container_sync(
                conn,
                user,
                container,
                direction="container_to_storage",
                storage_type="user_file",
                resource_id=None,
                storage_relative_path=rule["storage_relative_path"],
                container_path=rule["container_path"],
                conflict_policy=rule.get("conflict_policy") or "overwrite",
                rule_id=rule["id"],
            )
            conn.execute("UPDATE container_sync_rules SET last_run_at = %s, updated_at = %s WHERE id = %s", (now_ts(), now_ts(), rule_id))
            return {"sync_task": public_sync_task(sync_task), "node_task": public_task(node_task)}

    @app.post("/api/containers/{container_id}/ports", status_code=201)
    def create_container_port(container_id: int, payload: ContainerPortInput):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute(
                """
                SELECT c.*, n.allow_port_mapping, n.max_ports_per_container
                FROM containers c JOIN nodes n ON n.id = c.node_id
                WHERE c.id = %s
                """,
                (container_id,),
            ).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if not container["allow_port_mapping"]:
                raise HTTPException(status_code=400, detail="该节点不允许端口映射")
            existing_count = conn.execute(
                "SELECT COUNT(*) AS count FROM container_ports WHERE container_id = %s",
                (container_id,),
            ).fetchone()["count"]
            if existing_count >= container["max_ports_per_container"]:
                raise HTTPException(status_code=400, detail=f"该节点单容器最多允许 {container['max_ports_per_container']} 个端口映射")
            port = add_container_port(conn, container_id, payload)
            if container["status"] == "running":
                ports = list_container_ports(conn, container_id)
                if has_ssh_port(ports):
                    conn.execute(
                        "UPDATE containers SET access_status = 'pending', access_error = '', updated_at = %s WHERE id = %s",
                        (now_ts(), container_id),
                    )
                enqueue_node_task(
                    conn,
                    container["node_id"],
                    container_id,
                    "incus_sync_ports",
                    incus_ports_payload(container, ports),
                )
            audit(conn, "admin", "create", f"container-port:{port['id']}", {"container_id": container_id})
            return port

    @app.put("/api/containers/{container_id}/ports/{port_id}")
    def update_container_port(container_id: int, port_id: int, payload: ContainerPortInput):
        payload = normalize_port_payload(payload)
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            existing = conn.execute(
                "SELECT * FROM container_ports WHERE id = %s AND container_id = %s",
                (port_id, container_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="端口映射不存在")
            duplicated = conn.execute(
                """
                SELECT 1 FROM container_ports
                WHERE container_id = %s AND protocol = %s AND container_port = %s AND id != %s
                """,
                (container_id, payload.protocol, payload.container_port, port_id),
            ).fetchone()
            if duplicated:
                raise HTTPException(status_code=409, detail="该容器内端口映射已存在")
            port = conn.execute(
                """
                UPDATE container_ports
                SET name = %s, protocol = %s, container_port = %s, updated_at = %s
                WHERE id = %s AND container_id = %s
                RETURNING *
                """,
                (payload.name, payload.protocol, payload.container_port, now_ts(), port_id, container_id),
            ).fetchone()
            if container["status"] == "running":
                ports = list_container_ports(conn, container_id)
                if has_ssh_port(ports):
                    conn.execute(
                        "UPDATE containers SET access_status = 'pending', access_error = '', updated_at = %s WHERE id = %s",
                        (now_ts(), container_id),
                    )
                enqueue_node_task(
                    conn,
                    container["node_id"],
                    container_id,
                    "incus_sync_ports",
                    incus_ports_payload(container, ports),
                )
            audit(conn, "admin", "update", f"container-port:{port_id}", {"container_id": container_id})
            return port

    @app.delete("/api/containers/{container_id}/ports/{port_id}")
    def delete_container_port(container_id: int, port_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            port = conn.execute(
                "DELETE FROM container_ports WHERE id = %s AND container_id = %s RETURNING *",
                (port_id, container_id),
            ).fetchone()
            if not port:
                raise HTTPException(status_code=404, detail="端口映射不存在")
            if container["status"] == "running":
                ports = list_container_ports(conn, container_id)
                if has_ssh_port(ports):
                    conn.execute(
                        "UPDATE containers SET access_status = 'pending', access_error = '', updated_at = %s WHERE id = %s",
                        (now_ts(), container_id),
                    )
                enqueue_node_task(
                    conn,
                    container["node_id"],
                    container_id,
                    "incus_sync_ports",
                    incus_ports_payload(container, ports),
                )
            audit(conn, "admin", "delete", f"container-port:{port_id}", {"container_id": container_id})
            return {"ok": True}

    @app.delete("/api/containers/{container_id}")
    def delete_container(container_id: int, payload: ContainerDeleteRequest):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if payload.force and not is_admin_user(user):
                raise HTTPException(status_code=403, detail="仅管理员可强制移除容器记录")
            if payload.name != container["name"]:
                raise HTTPException(status_code=400, detail="容器名称确认不匹配")
            if container["status"] in ("provisioning", "starting", "stopping", "restarting", "deleting"):
                if not payload.force:
                    raise HTTPException(status_code=409, detail=f"容器正在 {container['status']}，请使用强制移除记录")
                conn.execute("DELETE FROM containers WHERE id = %s", (container_id,))
                audit(
                    conn,
                    "admin",
                    "force-delete-record",
                    f"container:{container_id}",
                    {"name": container["name"], "status": container["status"]},
                )
                return {"ok": True, "container_id": container_id}
            ts = now_ts()
            conn.execute("UPDATE containers SET status = 'deleting', updated_at = %s WHERE id = %s", (ts, container_id))
            enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                "incus_delete_container",
                incus_lifecycle_payload(container, "delete"),
            )
            audit(conn, "admin", "delete", f"container:{container_id}", {"name": container["name"]})
            return next(item for item in list_containers(conn) if item["id"] == container_id)
