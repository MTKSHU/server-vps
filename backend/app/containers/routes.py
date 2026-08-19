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
from ..images.policy import image_available_to_user
from ..platform_settings import effective_node_shared_storage_mode, get_platform_settings
from ..agent.tasks import get_node_task_event, release_node_task_event, signal_node_task_done  # noqa: F401 (signal_node_task_done re-exported for agent/routes)
from ..schemas import (
    ContainerCreate,
    ContainerDeleteRequest,
    ContainerPublicMountRemoveInput,
    ContainerNodeCacheSyncInput,
    ContainerNodeCacheMountInput,
    ContainerExecCreate,
    ContainerHomeMigrationInput,
    ContainerPortInput,
    ContainerPublishImageInput,
    ContainerResourceUpdate,
    ContainerResourceUploadInput,
    ContainerSyncInput,
    ContainerSyncRuleInput,
)
from ..auth import is_admin_user, require_admin
from ..data.routes import (
    _shared_resource_base_path,
    _shared_resource_storage_path,
    get_storage_settings,
    normalize_shared_resource_tags,
    public_shared_resource,
    upsert_tag_options,
)


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
    enqueue_incus_image_import_task = deps["enqueue_incus_image_import_task"]
    enqueue_node_task = deps["enqueue_node_task"]
    public_task = deps["public_task"]
    incus_create_payload = deps["incus_create_payload"]
    incus_lifecycle_payload = deps["incus_lifecycle_payload"]
    incus_ports_payload = deps["incus_ports_payload"]
    select_storage_node_for_path = deps["select_storage_node_for_path"]
    storage_root_for_node = deps["storage_root_for_node"]
    storage_image_base_name = deps["storage_image_base_name"]
    enqueue_resource_sync_task = deps.get("enqueue_resource_sync_task")

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
        task_type_override: str = "",
        extra_detail: dict[str, Any] | None = None,
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
        if extra_detail:
            detail.update(extra_detail)
        sync_task_type = task_type_override or ("shared_resource_sync" if resolved_resource_id else "user_home_sync")
        sync_task = conn.execute(
            """
            INSERT INTO data_sync_tasks (
                task_type, user_id, resource_id, source_node_id, target_node_id, container_id,
                source_path, target_path, status, detail, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'planned', %s, %s, %s)
            RETURNING *
            """,
            (
                sync_task_type,
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
        asyncio.create_task(workspace_cleanup_loop())

    async def workspace_cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                with db() as conn:
                    ts = now_ts()
                    rows = conn.execute(
                        "SELECT * FROM user_workspace_volumes v WHERE lifecycle='temporary' AND status='active' "
                        "AND cleanup_after>0 AND cleanup_after<=%s AND NOT EXISTS "
                        "(SELECT 1 FROM containers c WHERE c.owner_id=v.user_id AND c.node_id=v.node_id)",
                        (ts,),
                    ).fetchall()
                    for volume in rows:
                        task = enqueue_node_task(
                            conn, volume["node_id"], None, "remove_user_workspace_volume",
                            {"user_id": volume["user_id"], "node_id": volume["node_id"], "volume_name": volume["volume_name"]},
                        )
                        conn.execute(
                            "UPDATE user_workspace_volumes SET status='removing',updated_at=%s WHERE user_id=%s AND node_id=%s",
                            (ts, volume["user_id"], volume["node_id"]),
                        )
                        audit(conn, "system", "expire-workspace", f"workspace:{volume['volume_name']}", {"task_id": task["id"]})
            except Exception as exc:
                import sys
                print(f"[WARN] workspace cleanup: {exc!r}", file=sys.stderr, flush=True)

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
        if payload.name == "cluster-resource-downloader":
            raise HTTPException(status_code=400, detail="该名称保留给系统下载容器")
        payload.ssh_username = payload.ssh_username.strip() or "ubuntu"
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", payload.ssh_username):
            raise HTTPException(status_code=400, detail="初始用户名不合法")
        with db() as conn:
            user = current_user(conn)
            if payload.mounts and not is_admin_user(user):
                raise HTTPException(status_code=403, detail="普通用户不能提交宿主机挂载路径")
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
            if not image_available_to_user(image, user):
                raise HTTPException(status_code=403, detail="系统镜像仅供平台内部任务使用")
            allowed_node_ids = allowed_node_ids_for_user(conn, user)
            if payload.node_id is not None and allowed_node_ids is not None and payload.node_id not in allowed_node_ids:
                raise HTTPException(status_code=403, detail="当前用户不可使用该节点")
            settings = get_platform_settings(conn)
            node, selected_gpus, schedule_reasons = select_node_and_gpus(
                conn, image, payload, allowed_node_ids,
                shared_storage_settings=settings, user_id=user["id"],
            )
            if not node:
                detail = "没有满足资源、GPU 型号或镜像兼容性的节点"
                if schedule_reasons:
                    detail += "：" + "；".join(schedule_reasons[:5])
                raise HTTPException(status_code=400, detail=detail)
            shared_mode = effective_node_shared_storage_mode(node, settings, user["id"])
            shared_home_enabled = shared_mode == "enabled"
            node_capabilities = set(node.get("capabilities") or [])
            if shared_home_enabled and "managed_nfs_mounts_v1" not in node_capabilities:
                raise HTTPException(status_code=409, detail="目标节点 agent 尚不支持托管 NFS 挂载")
            if shared_home_enabled and "per_user_nfs_exports_v1" not in node_capabilities:
                raise HTTPException(status_code=409, detail="目标节点 agent 尚不支持每用户独立 NFS 导出，请先升级 agent")
            if shared_home_enabled and (
                not settings["nfs_server"] or not settings["nfs_sentinel_signature"]
            ):
                raise HTTPException(status_code=409, detail="该节点已启用共享存储，但平台 NFS 用户导出或 sentinel 尚未配置")
            # RootDisk 可按节点覆盖；0 表示继承平台默认值。workspace 卷仍单独计算。
            root_disk_gb = int(node.get("root_disk_gb") or CONTAINER_ROOT_DISK_GB)
            node_disk_limit = int(node.get("max_disk_gb_per_container") or 0)
            disk_total = int(node.get("disk_total_gb") or 0)
            reserved_floor = max(int(disk_total * 0.15), 100)
            node_disk_avail = max(0, disk_total - max(int(node.get("reserved_disk_gb") or 0), reserved_floor) - int(node.get("disk_used_gb") or 0))
            if node_disk_limit > 0 and root_disk_gb > node_disk_limit:
                raise HTTPException(status_code=400, detail=f"节点 RootDisk {root_disk_gb} GB 超过单容器磁盘上限 {node_disk_limit} GB")
            if root_disk_gb > node_disk_avail:
                raise HTTPException(status_code=400, detail=f"节点磁盘可用空间不足：RootDisk 需要 {root_disk_gb} GB，可用 {node_disk_avail} GB")
            # workspace_gb = min(节点上限, 用户配额上限)；如均为 0 则取节点可用磁盘
            workspace_default_gb = int(settings["workspace_default_gb"])
            if node_disk_limit > 0 and quota_data_vol_gb > 0:
                workspace_gb = min(workspace_default_gb, node_disk_limit, quota_data_vol_gb, node_disk_avail)
            elif node_disk_limit > 0:
                workspace_gb = min(workspace_default_gb, node_disk_limit, node_disk_avail)
            elif quota_data_vol_gb > 0:
                workspace_gb = min(workspace_default_gb, quota_data_vol_gb, node_disk_avail)
            else:
                workspace_gb = min(workspace_default_gb, node_disk_avail)
            if workspace_gb < 1:
                raise HTTPException(status_code=400, detail="节点本地磁盘已达到保留空间下限，无法创建 workspace")
            workspace_volume_name = f"user-{user['id']}-ws"
            ts = now_ts()
            conn.execute(
                """
                INSERT INTO user_workspace_volumes (
                    user_id, node_id, volume_name, quota_gb, status, last_error, created_at, updated_at, removed_at
                    , lifecycle, last_used_at, cleanup_after
                ) VALUES (%s, %s, %s, %s, 'active', '', %s, %s, 0, 'temporary', %s, 0)
                ON CONFLICT (user_id, node_id) DO UPDATE SET
                    volume_name = EXCLUDED.volume_name,
                    quota_gb = EXCLUDED.quota_gb,
                    status = 'active',
                    last_error = '',
                    updated_at = EXCLUDED.updated_at,
                    removed_at = 0
                """,
                (user["id"], node["id"], workspace_volume_name, workspace_gb, ts, ts, ts),
            )
            mounts = list(payload.mounts or [])
            managed_mounts: list[dict[str, Any]] = []
            if shared_home_enabled:
                dataset = conn.execute(
                    "SELECT * FROM user_storage_datasets WHERE user_id=%s AND status='applied'",
                    (user["id"],),
                ).fetchone()
                if not dataset:
                    raise HTTPException(status_code=409, detail="用户 ZFS dataset 尚未就绪")
                managed_mounts.append({
                    "kind": "user_home", "source": f"/var/lib/server-vps/nfs/user-datasets/{user['username']}",
                    "target": f"/home/{payload.ssh_username}", "readonly": False, "required": True,
                    "export": dataset.get("nfs_export_path") or dataset["mountpoint"],
                })
            for selection in payload.resources:
                resource = conn.execute(
                    "SELECT * FROM shared_resources WHERE id=%s AND enabled=TRUE", (selection.resource_id,),
                ).fetchone()
                if not resource:
                    raise HTTPException(status_code=400, detail=f"公开资源 {selection.resource_id} 不存在")
                target = validate_container_path(selection.mount_path.strip() or resource["mount_path"])
                cache = conn.execute(
                    "SELECT status,local_path FROM node_resource_cache WHERE node_id=%s AND resource_id=%s",
                    (node["id"], resource["id"]),
                ).fetchone()
                if cache and cache["status"] == "ready" and cache["local_path"]:
                    source, kind = cache["local_path"], "node_cache"
                else:
                    if not resource.get("nfs_available", True):
                        raise HTTPException(
                            status_code=409,
                            detail=f"资源 {resource['name']} 位于独立存储 dataset，目标节点本地缓存尚未就绪，不能通过父级 NFS 挂载",
                        )
                    if shared_mode == "disabled" or not settings["nfs_server"] or not settings["nfs_sentinel_signature"]:
                        raise HTTPException(status_code=409, detail=f"资源 {resource['name']} 无本地缓存且 NFS 未配置")
                    provider = str(resource["version"] or "default").strip("/")
                    base = "datasets" if resource["resource_type"] == "dataset" else "models"
                    source, kind = f"/var/lib/server-vps/nfs/{base}/{provider}/{resource['name']}", "shared_resource"
                managed_mounts.append({"kind": kind, "source": source, "target": target, "readonly": True, "required": True})
            if managed_mounts and "managed_nfs_mounts_v1" not in node_capabilities:
                raise HTTPException(status_code=409, detail="目标节点 agent 尚不支持托管挂载")
            mounts.extend(
                f"{item['source']}:{item['target']}{':ro' if item['readonly'] else ':rw'}" for item in managed_mounts
            )
            shared_storage = {
                "enabled": bool(managed_mounts), "server": settings["nfs_server"],
                "users_export": settings["nfs_users_export"], "datasets_export": settings["nfs_datasets_export"],
                "models_export": settings["nfs_models_export"], "mount_options": settings["nfs_mount_options"],
                "sentinel": settings["nfs_sentinel"], "sentinel_signature": settings["nfs_sentinel_signature"],
                "idmap_base": settings["nfs_idmap_base"],
            }
            container = conn.execute(
                """
                INSERT INTO containers (
                    name, owner_id, node_id, image_id, status, cpu_cores, memory_gb, disk_gb,
                    ssh_username, ssh_key, mounts, managed_mounts, ip, access_status, access_error, expires_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'provisioning', %s, %s, %s, %s, %s, %s, %s, %s, 'pending', '', %s, %s, %s)
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
                    Jsonb(managed_mounts),
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
                                     workspace_volume_name=workspace_volume_name, workspace_volume_gb=workspace_gb,
                                     managed_mounts=managed_mounts, shared_storage=shared_storage),
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
            if container.get("system_role"):
                raise HTTPException(status_code=409, detail="系统容器由节点 agent 管理，请在节点任务中维护")
            if container["status"] in ("provisioning", "starting", "stopping", "restarting", "deleting"):
                raise HTTPException(status_code=409, detail=f"容器正在 {container['status']}，请稍后再试")
            ts = now_ts()
            conn.execute("UPDATE containers SET status = %s, updated_at = %s WHERE id = %s", (pending_status, ts, container_id))
            settings = get_platform_settings(conn)
            managed_mounts = container.get("managed_mounts") or []
            shared_storage = {
                "enabled": bool(managed_mounts), "server": settings["nfs_server"],
                "users_export": settings["nfs_users_export"], "datasets_export": settings["nfs_datasets_export"],
                "models_export": settings["nfs_models_export"], "mount_options": settings["nfs_mount_options"],
                "sentinel": settings["nfs_sentinel"], "sentinel_signature": settings["nfs_sentinel_signature"],
                "idmap_base": settings["nfs_idmap_base"],
            }
            enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                f"incus_{operation}_container",
                incus_lifecycle_payload(container, operation, shared_storage),
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
            if container.get("system_role"):
                raise HTTPException(status_code=409, detail="系统容器由节点 agent 管理，不能重试创建")
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
            settings = get_platform_settings(conn)
            managed_mounts = container.get("managed_mounts") or []
            shared_storage = {
                "enabled": bool(managed_mounts), "server": settings["nfs_server"],
                "users_export": settings["nfs_users_export"], "datasets_export": settings["nfs_datasets_export"],
                "models_export": settings["nfs_models_export"], "mount_options": settings["nfs_mount_options"],
                "sentinel": settings["nfs_sentinel"], "sentinel_signature": settings["nfs_sentinel_signature"],
                "idmap_base": settings["nfs_idmap_base"],
            }
            enqueue_node_task(
                conn,
                node["id"],
                container_id,
                "incus_create_container",
                incus_create_payload(
                    container, image, node, selected_gpus, ports,
                    managed_mounts=managed_mounts, shared_storage=shared_storage,
                ),
            )
            audit(conn, user["username"], "retry", f"container:{container_id}", {"name": container["name"]})
            return next(item for item in list_containers(conn) if item["id"] == container_id)

    @app.post("/api/containers/{container_id}/migrate-home", status_code=202)
    def migrate_container_home(container_id: int, payload: ContainerHomeMigrationInput):
        actor = require_admin()
        with db() as conn:
            container = conn.execute(
                "SELECT c.*,u.username AS owner,n.capabilities,n.shared_storage_mode AS node_shared_storage_mode FROM containers c "
                "JOIN users u ON u.id=c.owner_id JOIN nodes n ON n.id=c.node_id WHERE c.id=%s",
                (container_id,),
            ).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            if container["status"] != "stopped":
                raise HTTPException(status_code=409, detail="迁移 Home 前必须停止容器")
            capabilities = set(container.get("capabilities") or [])
            if "managed_nfs_mounts_v1" not in capabilities:
                raise HTTPException(status_code=409, detail="节点 agent 不支持 NFS Home 迁移")
            if "per_user_nfs_exports_v1" not in capabilities:
                raise HTTPException(status_code=409, detail="节点 agent 不支持每用户独立 NFS 导出，请先升级 agent")
            dataset = conn.execute(
                "SELECT * FROM user_storage_datasets WHERE user_id=%s AND status='applied'",
                (container["owner_id"],),
            ).fetchone()
            if not dataset:
                raise HTTPException(status_code=409, detail="用户 ZFS dataset 尚未就绪")
            settings = get_platform_settings(conn)
            if effective_node_shared_storage_mode(
                {"shared_storage_mode": container["node_shared_storage_mode"]}, settings, container["owner_id"]
            ) != "enabled":
                raise HTTPException(status_code=409, detail="该节点已禁用共享存储，不能迁移 NFS Home")
            if not settings["nfs_server"] or not settings["nfs_sentinel_signature"]:
                raise HTTPException(status_code=409, detail="NFS 用户目录尚未配置")
            home_mount = {
                "kind": "user_home", "source": f"/var/lib/server-vps/nfs/user-datasets/{container['owner']}",
                "target": f"/home/{container['ssh_username']}", "readonly": False, "required": True,
                "export": dataset.get("nfs_export_path") or dataset["mountpoint"],
            }
            shared_storage = {
                "enabled": True, "server": settings["nfs_server"], "users_export": settings["nfs_users_export"],
                "datasets_export": settings["nfs_datasets_export"], "models_export": settings["nfs_models_export"],
                "mount_options": settings["nfs_mount_options"], "sentinel": settings["nfs_sentinel"],
                "sentinel_signature": settings["nfs_sentinel_signature"],
                "idmap_base": settings["nfs_idmap_base"],
            }
            task = enqueue_node_task(
                conn, container["node_id"], container_id, "migrate_container_home",
                {"container_id": container_id, "name": container["name"], "ssh_username": container["ssh_username"],
                 "owner": container["owner"], "primary": payload.primary, "managed_mount": home_mount,
                 "shared_storage": shared_storage},
            )
            audit(conn, actor["username"], "migrate-home", f"container:{container_id}", {"task_id": task["id"], "primary": payload.primary})
            return public_task(task)

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
                    "managed_mounts": container.get("managed_mounts") or [],
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

    @app.post("/api/containers/{container_id}/upload-as-resource", status_code=202)
    def upload_container_as_resource(container_id: int, payload: ContainerResourceUploadInput):
        """用户已在自己容器内下载/准备好数据后，一步注册为公开数据集/模型并从容器拉取到存储节点。

        复用 enqueue_container_sync 的 container_to_storage 链路（含跨节点临时密钥），
        暂存到与最终目录同级的 .{resource_id}.partial，成功后由
        migrate_shared_resource_path 原子切换到正式目录并自动触发校验。
        """
        name = payload.name.strip()
        version = payload.version.strip() or "default"
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", name):
            raise HTTPException(status_code=400, detail="资源名称不合法")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,80}", version):
            raise HTTPException(status_code=400, detail="资源提供者不合法")
        source = payload.source.strip().lower()
        if source not in ("", "huggingface", "modelscope"):
            raise HTTPException(status_code=400, detail="source 不合法，应为空、huggingface 或 modelscope")
        repo_id = payload.repo_id.strip()
        if source and not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}", repo_id):
            raise HTTPException(status_code=400, detail="仓库 ID 不合法，格式应为 owner/repo-name")
        revision = re.sub(r"[^\w./-]", "", payload.revision.strip()) or ("master" if source == "modelscope" else "main")
        tags = normalize_shared_resource_tags(payload.tags)

        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] != "running":
                raise HTTPException(status_code=400, detail="容器必须处于 running 状态才能上传")

            existing = conn.execute(
                "SELECT id, request_status, requested_by FROM shared_resources WHERE resource_type=%s AND name=%s AND version=%s",
                (payload.resource_type, name, version),
            ).fetchone()
            if existing and not (existing["requested_by"] == user["id"] and existing["request_status"] in ("uploading", "failed")):
                raise HTTPException(status_code=409, detail="同名的公开数据集/模型已存在，请更换名称或提供者")

            upsert_tag_options(conn, tags)
            settings = get_storage_settings(conn)
            base = _shared_resource_base_path(payload.resource_type, settings)
            mount_path = f"/datasets/{name}" if payload.resource_type == "dataset" else f"/models/{name}"
            final_platform_path = _shared_resource_storage_path(base, version, name)
            node = select_storage_node_for_path(conn, final_platform_path)
            if not node:
                raise HTTPException(status_code=400, detail="没有在线存储节点")

            source_url = f"{'hf' if source == 'huggingface' else 'ms'}://{repo_id}@{revision}" if source else ""
            ts = now_ts()
            if existing:
                resource_id = existing["id"]
                conn.execute(
                    """
                    UPDATE shared_resources
                    SET tags=%s, source_url=%s, mount_path=%s, request_status='uploading',
                        check_status='unknown', check_error='', updated_at=%s
                    WHERE id=%s
                    """,
                    (tags, source_url, mount_path, ts, resource_id),
                )
            else:
                row = conn.execute(
                    """
                    INSERT INTO shared_resources (
                        resource_type, name, version, source_path, mount_path, tags, readonly, sync_policy,
                        enabled, source_url, request_status, requested_by, check_status, check_error,
                        created_at, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,TRUE,'manual',TRUE,%s,'uploading',%s,'unknown','',%s,%s)
                    RETURNING id
                    """,
                    (payload.resource_type, name, version, final_platform_path, mount_path, tags, source_url, user["id"], ts, ts),
                ).fetchone()
                resource_id = row["id"]

            # 暂存目录与最终目录同级，确保未完成的上传不会出现在正式目录下
            staging_platform_path = f"{posixpath.dirname(final_platform_path).rstrip('/')}/.{resource_id}.partial"
            conn.execute("UPDATE shared_resources SET source_path=%s, updated_at=%s WHERE id=%s", (staging_platform_path, ts, resource_id))

            sync_task, node_task = enqueue_container_sync(
                conn,
                user,
                container,
                direction="container_to_storage",
                storage_type="dataset" if payload.resource_type == "dataset" else "model",
                resource_id=resource_id,
                storage_relative_path="",
                container_path=payload.container_path,
                conflict_policy=payload.conflict_policy,
                task_type_override="shared_resource_upload",
                extra_detail={
                    "final_platform_path": final_platform_path,
                    "final_local_path": source_path_for_node(final_platform_path, node),
                },
            )
            progress = {
                "phase": "uploading",
                "pct": 0,
                "task_id": node_task["id"],
                "sync_task_id": sync_task["id"],
                "node": node["hostname"],
                "container_id": container_id,
                "container_name": container["name"],
                "container_path": payload.container_path,
            }
            conn.execute(
                "UPDATE shared_resources SET download_progress=%s, updated_at=%s WHERE id=%s",
                (Jsonb(progress), ts, resource_id),
            )
            audit(conn, user["username"], "upload-as-resource", f"shared-resource:{resource_id}",
                  {"container_id": container_id, "container_path": payload.container_path, "sync_task_id": sync_task["id"]})
            resource_row = conn.execute("SELECT * FROM shared_resources WHERE id=%s", (resource_id,)).fetchone()
            return {
                "resource": public_shared_resource(resource_row),
                "sync_task": public_sync_task(sync_task),
                "node_task": public_task(node_task),
            }

    @app.get("/api/containers/node-cached-resources")
    def list_node_cached_resources(node_id: int):
        with db() as conn:
            user = current_user(conn)
            node = conn.execute("SELECT id FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="节点不存在")
            if not is_admin_user(user):
                allowed_node_ids = allowed_node_ids_for_user(conn, user)
                if allowed_node_ids is not None and node_id not in allowed_node_ids:
                    raise HTTPException(status_code=403, detail="当前用户不可使用该节点")
            rows = conn.execute(
                """
                SELECT sr.id, sr.resource_type, sr.name, sr.version, sr.mount_path,
                       sr.readonly, nrc.local_path
                FROM node_resource_cache nrc
                JOIN shared_resources sr ON sr.id = nrc.resource_id
                WHERE nrc.node_id = %s
                  AND nrc.status = 'ready'
                  AND COALESCE(nrc.local_path, '') != ''
                  AND sr.enabled = TRUE
                ORDER BY sr.resource_type, sr.name, sr.version
                """,
                (node_id,),
            ).fetchall()
            return rows

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

    @app.post("/api/containers/{container_id}/apply-node-cache", status_code=202)
    def apply_container_node_cache(container_id: int):
        """将容器的资源挂载切换为本地缓存路径（如果已同步完成）。"""
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            node_id = container["node_id"]
            current_mounts = list(container["mounts"] or [])

            # 获取所有共享资源：source_path → 资源完整信息
            source_to_resource = {r["source_path"]: r for r in conn.execute(
                "SELECT id, source_path, mount_path FROM shared_resources WHERE enabled = TRUE"
            ).fetchall()}

            # 获取该节点已就绪的本地缓存
            caches = conn.execute(
                "SELECT resource_id, local_path FROM node_resource_cache"
                " WHERE node_id = %s AND status = 'ready' AND local_path != ''",
                (node_id,),
            ).fetchall()
            rid_to_local = {c["resource_id"]: c["local_path"] for c in caches}

            new_mounts = []
            mount_updates = []
            changed = False
            for mount_str in current_mounts:
                readonly = mount_str.endswith(":ro")
                core = mount_str[:-3] if readonly else mount_str
                parts = core.split(":", 1)
                source = parts[0]
                mount_path = parts[1] if len(parts) == 2 else source
                resource = source_to_resource.get(source)
                rid = resource["id"] if resource else None
                if rid and rid in rid_to_local:
                    new_source = rid_to_local[rid]
                    new_target = resource["mount_path"]
                    suffix = ":ro" if readonly else ""
                    new_mounts.append(f"{new_source}:{new_target}{suffix}")
                    mount_updates.append({
                        "old_target": mount_path,
                        "new_source": new_source,
                        "new_target": new_target,
                        "readonly": readonly,
                    })
                    changed = True
                else:
                    new_mounts.append(mount_str)

            if not changed:
                raise HTTPException(status_code=409, detail="没有可更新的资源挂载，本地缓存尚未就绪或挂载路径不匹配")

            conn.execute(
                "UPDATE containers SET mounts = %s, updated_at = %s WHERE id = %s",
                (Jsonb(new_mounts), now_ts(), container_id),
            )
            task = enqueue_node_task(
                conn,
                node_id,
                container_id,
                "apply_resource_mounts",
                {"container_id": container_id, "name": container["name"], "mount_updates": mount_updates},
            )
            return public_task(task)

    @app.get("/api/containers/{container_id}/node-cached-resources")
    def container_node_cached_resources(container_id: int):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            rows = conn.execute(
                """
                SELECT sr.id, sr.resource_type, sr.name, sr.version, sr.mount_path,
                       sr.readonly, nrc.local_path
                FROM node_resource_cache nrc
                JOIN shared_resources sr ON sr.id = nrc.resource_id
                WHERE nrc.node_id = %s
                  AND nrc.status = 'ready'
                  AND COALESCE(nrc.local_path, '') != ''
                  AND sr.enabled = TRUE
                ORDER BY sr.resource_type, sr.name, sr.version
                """,
                (container["node_id"],),
            ).fetchall()
            return rows

    @app.post("/api/containers/{container_id}/mount-public-resources", status_code=202)
    def mount_container_public_resources(container_id: int, payload: ContainerNodeCacheMountInput):
        """给已有容器追加只读公开资源；节点缓存就绪时优先缓存，否则使用托管 NFS。"""
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] not in ("running", "stopped"):
                raise HTTPException(status_code=409, detail="容器必须是 running 或 stopped 状态才能追加公开资源")

            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (container["node_id"],)).fetchone()
            if not node or node["status"] != "online":
                raise HTTPException(status_code=409, detail="目标节点当前不在线")
            capabilities = set(node.get("capabilities") or [])
            if "managed_nfs_hot_mounts_v1" not in capabilities:
                raise HTTPException(status_code=409, detail="目标节点 agent 尚不支持安全的公开资源热挂载，请先升级 agent")

            requested_ids = list(dict.fromkeys(resource_id for resource_id in payload.resource_ids if resource_id > 0))
            if not requested_ids:
                raise HTTPException(status_code=400, detail="请先选择至少一个公开资源")
            resources = conn.execute(
                """
                SELECT * FROM shared_resources
                WHERE enabled = TRUE AND request_status = 'ready' AND id = ANY(%s::bigint[])
                ORDER BY resource_type, name
                """,
                (requested_ids,),
            ).fetchall()
            if len(resources) != len(requested_ids):
                raise HTTPException(status_code=400, detail="部分公开资源不存在、未启用或尚未就绪")

            settings = get_platform_settings(conn)
            shared_mode = effective_node_shared_storage_mode(node, settings, container["owner_id"])
            caches = conn.execute(
                """
                SELECT resource_id, status, local_path FROM node_resource_cache
                WHERE node_id = %s AND resource_id = ANY(%s::bigint[])
                """,
                (node["id"], requested_ids),
            ).fetchall()
            cache_by_resource = {row["resource_id"]: row for row in caches}

            existing_managed = list(container.get("managed_mounts") or [])
            managed_by_target = {str(item.get("target") or ""): item for item in existing_managed}
            existing_mounts = list(container.get("mounts") or [])

            def split_mount(value: str) -> tuple[str, str, bool]:
                readonly = value.endswith(":ro")
                core = value[:-3] if readonly else value[:-3] if value.endswith(":rw") else value
                parts = core.split(":", 1)
                return parts[0], parts[1] if len(parts) == 2 else parts[0], readonly

            mounts_by_target = {split_mount(value)[1]: value for value in existing_mounts}
            resource_rows = {row["id"]: row for row in resources}
            resolved: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for resource_id in requested_ids:
                resource = resource_rows[resource_id]
                target = validate_container_path(resource["mount_path"])
                cache = cache_by_resource.get(resource_id)
                if cache and cache["status"] == "ready" and cache["local_path"]:
                    source, kind = cache["local_path"], "node_cache"
                else:
                    if shared_mode != "enabled":
                        raise HTTPException(
                            status_code=409,
                            detail=f"资源 {resource['name']} 无本地缓存，且该用户在目标节点未启用共享存储",
                        )
                    if not resource.get("nfs_available", True):
                        raise HTTPException(status_code=409, detail=f"资源 {resource['name']} 不允许通过父级 NFS 挂载")
                    if not settings["nfs_server"] or not settings["nfs_sentinel_signature"]:
                        raise HTTPException(status_code=409, detail="平台 NFS 服务地址或 sentinel 尚未配置")
                    provider = str(resource["version"] or "default").strip("/")
                    base = "datasets" if resource["resource_type"] == "dataset" else "models"
                    source, kind = f"/var/lib/server-vps/nfs/{base}/{provider}/{resource['name']}", "shared_resource"

                occupied = managed_by_target.get(target)
                if target in mounts_by_target and not occupied:
                    linked = conn.execute(
                        "SELECT 1 FROM container_resources WHERE container_id=%s AND mount_path=%s",
                        (container_id, target),
                    ).fetchone()
                    if not linked:
                        raise HTTPException(status_code=409, detail=f"容器路径 {target} 已被其他挂载占用")
                if occupied and occupied.get("kind") not in ("shared_resource", "node_cache"):
                    raise HTTPException(status_code=409, detail=f"容器路径 {target} 已被 {occupied.get('kind')} 挂载占用")
                resolved.append((resource, {
                    "kind": kind, "source": source, "target": target, "readonly": True, "required": True,
                }))

            replacements = {item["target"]: item for _, item in resolved}
            new_managed = [item for item in existing_managed if item.get("target") not in replacements]
            new_managed.extend(replacements.values())
            new_mounts = [value for value in existing_mounts if split_mount(value)[1] not in replacements]
            new_mounts.extend(f"{item['source']}:{item['target']}:ro" for item in replacements.values())

            mount_updates: list[dict[str, Any]] = []
            for _, item in resolved:
                old = managed_by_target.get(item["target"])
                old_mount = mounts_by_target.get(item["target"])
                desired_mount = f"{item['source']}:{item['target']}:ro"
                if old == item and old_mount == desired_mount:
                    continue
                mount_updates.append({
                    "old_target": item["target"] if old_mount else "",
                    "new_source": item["source"],
                    "new_target": item["target"],
                    "readonly": True,
                })
            if not mount_updates:
                raise HTTPException(status_code=409, detail="所选公开资源已经按当前最优来源挂载")

            shared_storage = {
                "enabled": True,
                "server": settings["nfs_server"],
                "users_export": settings["nfs_users_export"],
                "datasets_export": settings["nfs_datasets_export"],
                "models_export": settings["nfs_models_export"],
                "mount_options": settings["nfs_mount_options"],
                "sentinel": settings["nfs_sentinel"],
                "sentinel_signature": settings["nfs_sentinel_signature"],
                "idmap_base": settings["nfs_idmap_base"],
            }
            ts = now_ts()
            conn.execute(
                "UPDATE containers SET mounts=%s, managed_mounts=%s, updated_at=%s WHERE id=%s",
                (Jsonb(new_mounts), Jsonb(new_managed), ts, container_id),
            )
            for resource, item in resolved:
                conn.execute(
                    """
                    INSERT INTO container_resources (container_id, resource_id, mount_path, created_at)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (container_id, resource_id) DO UPDATE SET mount_path=EXCLUDED.mount_path
                    """,
                    (container_id, resource["id"], item["target"], ts),
                )
            task = enqueue_node_task(
                conn,
                node["id"],
                container_id,
                "apply_resource_mounts",
                {
                    "container_id": container_id,
                    "name": container["name"],
                    "mount_updates": mount_updates,
                    "managed_mounts": new_managed,
                    "shared_storage": shared_storage,
                },
            )
            audit(
                conn,
                user["username"],
                "mount-public-resources",
                f"container:{container_id}",
                {"resource_ids": requested_ids, "task_id": task["id"], "sources": [item["kind"] for _, item in resolved]},
            )
            return public_task(task)

    @app.post("/api/containers/{container_id}/mount-node-cache", status_code=202)
    def mount_container_node_cache(container_id: int, payload: ContainerNodeCacheMountInput):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] not in ("running", "stopped"):
                raise HTTPException(status_code=400, detail="容器必须是 running 或 stopped 状态才能挂载公开资源")

            requested_ids = list(dict.fromkeys(resource_id for resource_id in payload.resource_ids if resource_id > 0))
            if not requested_ids:
                raise HTTPException(status_code=400, detail="请先选择至少一个公开资源")

            resources = conn.execute(
                """
                SELECT sr.id, sr.name, sr.resource_type, sr.version, sr.mount_path,
                       sr.readonly, nrc.local_path
                FROM node_resource_cache nrc
                JOIN shared_resources sr ON sr.id = nrc.resource_id
                WHERE nrc.node_id = %s
                  AND nrc.status = 'ready'
                  AND COALESCE(nrc.local_path, '') != ''
                  AND sr.enabled = TRUE
                  AND sr.id = ANY(%s::bigint[])
                ORDER BY sr.resource_type, sr.name
                """,
                (container["node_id"], requested_ids),
            ).fetchall()
            if len(resources) != len(requested_ids):
                raise HTTPException(status_code=400, detail="部分资源尚未同步到当前容器所在节点，请先在存储中心触发同步")

            existing_mounts = list(container.get("mounts") or [])
            updates_by_target: dict[str, dict[str, Any]] = {}
            resource_by_id = {item["id"]: item for item in resources}
            for resource_id in requested_ids:
                resource = resource_by_id[resource_id]
                target = resource["mount_path"]
                updates_by_target[target] = {
                    "new_source": resource["local_path"],
                    "new_target": target,
                    "readonly": bool(resource["readonly"]),
                    "old_target": target,
                    "resource_id": resource["id"],
                }

            new_mounts: list[str] = []
            mount_updates: list[dict[str, Any]] = []
            seen_targets: set[str] = set()
            changed = False

            for mount_str in existing_mounts:
                readonly = mount_str.endswith(":ro")
                core = mount_str[:-3] if readonly else mount_str
                parts = core.split(":", 1)
                source = parts[0]
                target = parts[1] if len(parts) == 2 else source
                update = updates_by_target.get(target)
                if not update:
                    new_mounts.append(mount_str)
                    continue
                seen_targets.add(target)
                final_readonly = bool(update["readonly"])
                suffix = ":ro" if final_readonly else ""
                new_mount = f"{update['new_source']}:{target}{suffix}"
                new_mounts.append(new_mount)
                if new_mount != mount_str:
                    mount_updates.append(
                        {
                            "old_target": target,
                            "new_source": update["new_source"],
                            "new_target": target,
                            "readonly": final_readonly,
                        }
                    )
                    changed = True

            for target, update in updates_by_target.items():
                if target in seen_targets:
                    continue
                suffix = ":ro" if update["readonly"] else ""
                new_mounts.append(f"{update['new_source']}:{target}{suffix}")
                mount_updates.append(
                    {
                        "old_target": "",
                        "new_source": update["new_source"],
                        "new_target": target,
                        "readonly": bool(update["readonly"]),
                    }
                )
                changed = True

            if not changed:
                raise HTTPException(status_code=409, detail="所选资源已挂载到容器，无需重复操作")

            ts = now_ts()
            conn.execute(
                "UPDATE containers SET mounts = %s, updated_at = %s WHERE id = %s",
                (Jsonb(new_mounts), ts, container_id),
            )
            for resource_id in requested_ids:
                mount_path = resource_by_id[resource_id]["mount_path"]
                conn.execute(
                    """
                    INSERT INTO container_resources (container_id, resource_id, mount_path, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (container_id, resource_id) DO UPDATE SET
                        mount_path = EXCLUDED.mount_path
                    """,
                    (container_id, resource_id, mount_path, ts),
                )

            task = enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                "apply_resource_mounts",
                {"container_id": container_id, "name": container["name"], "mount_updates": mount_updates},
            )
            audit(
                conn,
                user["username"],
                "mount-node-cache",
                f"container:{container_id}",
                {"resource_ids": requested_ids, "task_id": task["id"]},
            )
            return public_task(task)

    @app.post("/api/containers/{container_id}/unmount-public-resources", status_code=202)
    def unmount_container_public_resources(container_id: int, payload: ContainerPublicMountRemoveInput):
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            if container["status"] not in ("running", "stopped"):
                raise HTTPException(status_code=400, detail="容器必须是 running 或 stopped 状态才能移除公开资源挂载")

            requested_mounts = [validate_container_path(path) for path in (payload.mount_paths or []) if str(path).strip()]
            requested_mounts = list(dict.fromkeys(requested_mounts))
            if not requested_mounts:
                raise HTTPException(status_code=400, detail="请先选择至少一个挂载路径")

            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (container["node_id"],)).fetchone()
            if not node or node["status"] != "online":
                raise HTTPException(status_code=409, detail="目标节点当前不在线")

            existing_managed = list(container.get("managed_mounts") or [])
            removable_kinds = {"shared_resource", "node_cache"}
            removable_targets = {
                str(item.get("target") or "")
                for item in existing_managed
                if str(item.get("kind") or "") in removable_kinds
            }
            targets = [target for target in requested_mounts if target in removable_targets]
            if not targets:
                raise HTTPException(status_code=404, detail="未找到可移除的公开资源挂载")

            def split_mount(value: str) -> tuple[str, str, bool]:
                readonly = value.endswith(":ro")
                core = value[:-3] if readonly else value[:-3] if value.endswith(":rw") else value
                parts = core.split(":", 1)
                return parts[0], parts[1] if len(parts) == 2 else parts[0], readonly

            existing_mounts = list(container.get("mounts") or [])
            new_managed = [item for item in existing_managed if str(item.get("target") or "") not in targets]
            new_mounts = [value for value in existing_mounts if split_mount(value)[1] not in targets]

            if len(new_managed) == len(existing_managed) and len(new_mounts) == len(existing_mounts):
                raise HTTPException(status_code=409, detail="所选挂载已不存在，无需重复移除")

            ts = now_ts()
            conn.execute(
                "UPDATE containers SET mounts=%s, managed_mounts=%s, updated_at=%s WHERE id=%s",
                (Jsonb(new_mounts), Jsonb(new_managed), ts, container_id),
            )
            conn.execute(
                "DELETE FROM container_resources WHERE container_id=%s AND mount_path = ANY(%s::text[])",
                (container_id, targets),
            )

            task = enqueue_node_task(
                conn,
                node["id"],
                container_id,
                "remove_resource_mounts",
                {
                    "container_id": container_id,
                    "name": container["name"],
                    "targets": targets,
                },
            )
            audit(
                conn,
                user["username"],
                "unmount-public-resources",
                f"container:{container_id}",
                {"mount_paths": targets, "task_id": task["id"]},
            )
            return public_task(task)

    @app.post("/api/containers/{container_id}/sync-resource/{resource_id}", status_code=202)
    def container_sync_and_mount_resource(container_id: int, resource_id: int):
        """触发将公开资源同步到容器所在节点，完成后自动挂载到该容器。"""
        if enqueue_resource_sync_task is None:
            raise HTTPException(status_code=503, detail="同步服务未配置")
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)
            task = enqueue_resource_sync_task(
                conn, container["node_id"], resource_id, container_id=container_id
            )
            if task is None:
                raise HTTPException(status_code=409, detail="同步任务已在进行中，或节点/资源不可用")
            return public_task(task)

    @app.post("/api/containers/{container_id}/sync-node-cache", status_code=202)
    def container_sync_node_cache(container_id: int, payload: ContainerNodeCacheSyncInput):
        """按容器所在节点批量同步公开资源到本地缓存。"""
        if enqueue_resource_sync_task is None:
            raise HTTPException(status_code=503, detail="同步服务未配置")
        with db() as conn:
            user = current_user(conn)
            container = conn.execute("SELECT * FROM containers WHERE id = %s", (container_id,)).fetchone()
            if not container:
                raise HTTPException(status_code=404, detail="容器不存在")
            require_container_access(user, container)

            requested_ids = list(dict.fromkeys(resource_id for resource_id in payload.resource_ids if resource_id > 0))
            if not requested_ids:
                raise HTTPException(status_code=400, detail="请先选择至少一个公开资源")

            valid_rows = conn.execute(
                """
                SELECT id
                FROM shared_resources
                WHERE enabled = TRUE
                  AND id = ANY(%s::bigint[])
                """,
                (requested_ids,),
            ).fetchall()
            valid_ids = {row["id"] for row in valid_rows}
            if len(valid_ids) != len(requested_ids):
                raise HTTPException(status_code=400, detail="部分公开资源不存在或未启用")

            tasks: list[dict[str, Any]] = []
            skipped_resource_ids: list[int] = []
            for resource_id in requested_ids:
                task = enqueue_resource_sync_task(conn, container["node_id"], resource_id, container_id=container_id)
                if task is None:
                    skipped_resource_ids.append(resource_id)
                    continue
                tasks.append(public_task(task))

            if not tasks:
                raise HTTPException(status_code=409, detail="所选资源均已有进行中的同步任务")

            audit(
                conn,
                user["username"],
                "sync-node-cache",
                f"container:{container_id}",
                {
                    "resource_ids": requested_ids,
                    "submitted_task_ids": [task["id"] for task in tasks],
                    "skipped_resource_ids": skipped_resource_ids,
                },
            )
            return {
                "tasks": tasks,
                "submitted_count": len(tasks),
                "skipped_resource_ids": skipped_resource_ids,
            }

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
            if container.get("system_role"):
                raise HTTPException(status_code=409, detail="系统容器由节点 agent 管理，不能从容器管理页删除")
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
            settings = get_platform_settings(conn)
            managed_mounts = container.get("managed_mounts") or []
            shared_storage = {
                "enabled": bool(managed_mounts), "server": settings["nfs_server"],
                "users_export": settings["nfs_users_export"], "datasets_export": settings["nfs_datasets_export"],
                "models_export": settings["nfs_models_export"], "mount_options": settings["nfs_mount_options"],
                "sentinel": settings["nfs_sentinel"], "sentinel_signature": settings["nfs_sentinel_signature"],
                "idmap_base": settings["nfs_idmap_base"],
            }
            enqueue_node_task(
                conn,
                container["node_id"],
                container_id,
                "incus_delete_container",
                incus_lifecycle_payload(container, "delete", shared_storage),
            )
            audit(conn, "admin", "delete", f"container:{container_id}", {"name": container["name"]})
            return next(item for item in list_containers(conn) if item["id"] == container_id)
