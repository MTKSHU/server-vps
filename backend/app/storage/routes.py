import asyncio
import logging
import os
import shlex
from typing import Any

logger = logging.getLogger(__name__)

import asyncssh
from fastapi import HTTPException
from pydantic import BaseModel
from ..auth import require_admin

from ..config import AGENT_RELEASE_DIR, SYNC_SSH_IDENTITY_FILE, SYNC_SSH_PORT, SYNC_SSH_USER
from ..schemas import StorageImageDistributeInput, StorageImageExportInput


def register_storage_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    mark_stale_nodes = deps["mark_stale_nodes"]
    public_storage_image_file = deps["public_storage_image_file"]
    current_user = deps["current_user"]
    find_node_incus_image = deps["find_node_incus_image"]
    storage_image_base_name = deps["storage_image_base_name"]
    storage_root_for_node = deps["storage_root_for_node"]
    now_ts = deps["now_ts"]
    enqueue_node_task = deps["enqueue_node_task"]
    audit = deps["audit"]
    public_task = deps["public_task"]
    node_has_incus_image = deps["node_has_incus_image"]
    incus_image_import_payload = deps["incus_image_import_payload"]
    ensure_user_zfs_dataset_task = deps.get("ensure_user_zfs_dataset_task")
    remove_user_zfs_dataset_task = deps.get("remove_user_zfs_dataset_task")
    remove_user_workspace_volume_task = deps.get("remove_user_workspace_volume_task")
    enqueue_resource_sync_task = deps.get("enqueue_resource_sync_task")

    class RemoveWorkspaceVolumeInput(BaseModel):
        confirm_username: str = ""
        confirm_volume_name: str = ""

    def node_ssh_kwargs(node: dict[str, Any]) -> dict[str, Any]:
        ssh_host = str(node.get("ip") or "").strip()
        if not ssh_host:
            raise HTTPException(status_code=400, detail="节点地址缺失")
        kwargs: dict[str, Any] = {
            "host": ssh_host,
            "port": int(node.get("ssh_port") or SYNC_SSH_PORT),
            "username": str(node.get("ssh_user") or SYNC_SSH_USER),
            "known_hosts": None,
        }
        identity = ""
        if SYNC_SSH_IDENTITY_FILE and os.path.isfile(SYNC_SSH_IDENTITY_FILE):
            identity = SYNC_SSH_IDENTITY_FILE
        else:
            cluster_key = os.path.join(AGENT_RELEASE_DIR, ".cluster_node_key")
            if os.path.isfile(cluster_key):
                identity = cluster_key
        if identity:
            kwargs["client_keys"] = [identity]
        return kwargs

    async def workspace_volume_used_gb(row: dict[str, Any]) -> float | None:
        if row.get("node_status") != "online" or not row.get("volume_name"):
            return None
        volume_name = row["volume_name"]
        try:
            async with asyncssh.connect(**node_ssh_kwargs(row), connect_timeout=10) as ssh:
                # Search ZFS datasets by name pattern directly.
                # Avoids relying on `incus` being in $PATH for non-interactive SSH
                # sessions (e.g. snap-installed incus is under /snap/bin which is
                # not in the default non-interactive PATH), and handles any ZFS
                # pool / source name without extra round-trips.
                grep_pat = shlex.quote(f"/custom/default_{volume_name}")
                result = await ssh.run(
                    f"zfs list -Hp -o name,used -t filesystem,volume 2>/dev/null | grep -E {grep_pat}",
                    check=False,
                )
                lines = (result.stdout or "").strip().splitlines()
                if not lines:
                    return None
                parts = lines[0].split("\t")
                if len(parts) < 2:
                    return None
                return round(max(0, int(parts[1])) / 1024 / 1024 / 1024, 2)
        except Exception as exc:
            logger.debug("workspace_volume_used_gb(%s): %s", volume_name, exc)
            return None

    @app.get("/api/storage/volumes")
    def storage_volumes():
        with db() as conn:
            mark_stale_nodes(conn)
            rows = conn.execute(
                """
                SELECT svr.*, n.hostname, n.ip, n.node_type, n.status AS node_status, n.last_seen
                FROM storage_volume_reports svr
                JOIN nodes n ON n.id = svr.node_id
                WHERE n.node_type IN ('storage', 'mixed')
                  AND svr.volume_name IN ('root', 'users', 'datasets', 'models', 'backups')
                ORDER BY
                    CASE n.node_type WHEN 'storage' THEN 1 WHEN 'mixed' THEN 2 ELSE 3 END,
                    n.hostname,
                    CASE svr.volume_name
                      WHEN 'root' THEN 1
                      WHEN 'users' THEN 2
                      WHEN 'datasets' THEN 3
                      WHEN 'models' THEN 4
                      WHEN 'backups' THEN 5
                      ELSE 99
                    END,
                    svr.volume_name
                """
            ).fetchall()
            return rows

    @app.get("/api/storage/images")
    def storage_images():
        with db() as conn:
            mark_stale_nodes(conn)
            files = conn.execute(
                """
                SELECT sif.*, n.hostname AS source_node, n.status AS source_node_status,
                       u.username AS owner
                FROM storage_image_files sif
                JOIN nodes n ON n.id = sif.source_node_id
                LEFT JOIN users u ON u.id = sif.owner_id
                ORDER BY sif.updated_at DESC, sif.id DESC
                """
            ).fetchall()
            inventory = conn.execute(
                """
                SELECT nii.*, n.hostname AS node, n.node_type, n.status AS node_status
                FROM node_incus_images nii
                JOIN nodes n ON n.id = nii.node_id
                WHERE n.node_type IN ('storage', 'mixed')
                ORDER BY n.hostname, nii.aliases, nii.description, nii.fingerprint
                """
            ).fetchall()
            return {
                "files": [public_storage_image_file(row) for row in files],
                "inventory": [
                    {
                        "node_id": row["node_id"],
                        "node": row["node"],
                        "node_type": row["node_type"],
                        "node_status": row["node_status"],
                        "fingerprint": row["fingerprint"],
                        "aliases": row["aliases"],
                        "description": row["description"],
                        "architecture": row["architecture"],
                        "updated_at": row["updated_at"],
                    }
                    for row in inventory
                ],
            }

    @app.get("/api/storage/user-datasets")
    def user_storage_datasets():
        require_admin()
        with db() as conn:
            rows = conn.execute(
                """
                SELECT u.id AS user_id, u.username, u.display_name, u.enabled,
                       udp.home_path, q.storage_quota_gb,
                       usd.node_id, usd.dataset_name, usd.mountpoint, usd.quota_gb,
                       usd.status, usd.last_error, usd.applied_at, usd.updated_at,
                       n.hostname AS node, n.status AS node_status
                FROM users u
                LEFT JOIN user_data_policies udp ON udp.user_id = u.id
                LEFT JOIN quotas q ON q.user_id = u.id
                LEFT JOIN user_storage_datasets usd ON usd.user_id = u.id
                LEFT JOIN nodes n ON n.id = usd.node_id
                ORDER BY u.username
                """
            ).fetchall()
            return rows

    @app.post("/api/storage/user-datasets/{user_id}/ensure", status_code=202)
    def ensure_user_storage_dataset(user_id: int):
        require_admin()
        if not ensure_user_zfs_dataset_task:
            raise HTTPException(status_code=500, detail="ZFS dataset 任务未配置")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="用户不存在")
            task = ensure_user_zfs_dataset_task(conn, user_id, "manual")
            audit(conn, "admin", "ensure-zfs", f"user:{user_id}", {"task_id": task["id"] if task else 0})
            return {"task": public_task(task) if task else None}

    @app.post("/api/storage/user-datasets/ensure-all", status_code=202)
    def ensure_all_user_storage_datasets():
        require_admin()
        if not ensure_user_zfs_dataset_task:
            raise HTTPException(status_code=500, detail="ZFS dataset 任务未配置")
        with db() as conn:
            users = conn.execute("SELECT id FROM users WHERE enabled = TRUE ORDER BY id").fetchall()
            tasks = []
            for user in users:
                task = ensure_user_zfs_dataset_task(conn, user["id"], "manual-all")
                if task:
                    tasks.append(public_task(task))
            audit(conn, "admin", "ensure-zfs-all", "user-storage-datasets", {"task_count": len(tasks)})
            return {"tasks": tasks}

    @app.delete("/api/storage/user-datasets/{user_id}", status_code=202)
    def remove_user_storage_dataset(user_id: int):
        require_admin()
        if not remove_user_zfs_dataset_task:
            raise HTTPException(status_code=500, detail="ZFS dataset 移除任务未配置")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="用户不存在")
            if user["enabled"]:
                raise HTTPException(status_code=400, detail="只能移除未启用用户的 ZFS dataset")
            task = remove_user_zfs_dataset_task(conn, user_id)
            audit(conn, "admin", "remove-zfs", f"user:{user_id}", {"task_id": task["id"] if task else 0})
            return {"task": public_task(task) if task else None}

    @app.get("/api/storage/workspace-volumes")
    async def workspace_volumes(fetch_disk_usage: bool = False):
        require_admin()
        with db() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT uwv.*, u.username, u.display_name, u.enabled,
                       n.hostname AS node, n.status AS node_status, n.ip, n.ssh_user, n.ssh_port,
                       COALESCE(c.active_container_count, 0) AS active_container_count
                FROM user_workspace_volumes uwv
                JOIN users u ON u.id = uwv.user_id
                JOIN nodes n ON n.id = uwv.node_id
                LEFT JOIN (
                    SELECT owner_id, node_id, COUNT(*) AS active_container_count
                    FROM containers
                    WHERE status != 'deleting'
                    GROUP BY owner_id, node_id
                ) c ON c.owner_id = uwv.user_id AND c.node_id = uwv.node_id
                ORDER BY n.hostname, u.username
                """
            ).fetchall()]
        if fetch_disk_usage:
            used_values = await asyncio.gather(*(workspace_volume_used_gb(row) for row in rows))
            for row, used_gb in zip(rows, used_values):
                row["used_gb"] = used_gb
        else:
            for row in rows:
                row["used_gb"] = None
        for row in rows:
            row.pop("ip", None)
            row.pop("ssh_user", None)
            row.pop("ssh_port", None)
        return rows

    @app.post("/api/storage/workspace-volumes/{node_id}/{user_id}/remove", status_code=202)
    def remove_workspace_volume(node_id: int, user_id: int, payload: RemoveWorkspaceVolumeInput):
        require_admin()
        if not remove_user_workspace_volume_task:
            raise HTTPException(status_code=500, detail="用户节点数据卷回收任务未配置")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="用户不存在")
            volume = conn.execute(
                "SELECT * FROM user_workspace_volumes WHERE user_id = %s AND node_id = %s",
                (user_id, node_id),
            ).fetchone()
            if not volume or volume["status"] == "removed":
                raise HTTPException(status_code=404, detail="用户节点数据卷不存在")
            confirmed_name = (payload.confirm_volume_name or payload.confirm_username).strip()
            if confirmed_name != volume["volume_name"]:
                raise HTTPException(status_code=400, detail="请输入数据卷名确认回收")
            task = remove_user_workspace_volume_task(conn, user_id, node_id)
            audit(conn, "admin", "remove-workspace-volume", f"user:{user_id}:node:{node_id}", {"task_id": task["id"] if task else 0})
            return {"task": public_task(task) if task else None}

    @app.post("/api/storage/images/export", status_code=202)
    def export_storage_image(payload: StorageImageExportInput):
        require_admin()
        image_ref = payload.image_ref.strip()
        if not image_ref:
            raise HTTPException(status_code=400, detail="Incus 镜像引用不能为空")
        with db() as conn:
            actor = current_user(conn)
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (payload.source_node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="源节点不存在")
            if node["node_type"] not in ("storage", "mixed"):
                raise HTTPException(status_code=400, detail="只能从 storage/mixed 节点导出仓库镜像")
            if node["status"] != "online" or node["incus_status"] in ("unavailable", ""):
                raise HTTPException(status_code=400, detail="源节点必须 online 且 Incus 可用")
            incus_image = find_node_incus_image(conn, node["id"], image_ref)
            if not incus_image:
                raise HTTPException(status_code=404, detail="源节点 Incus 库存中没有该镜像")
            aliases = [alias.strip() for alias in incus_image["aliases"].split(",") if alias.strip()]
            alias = payload.alias.strip() or (aliases[0] if aliases else image_ref)
            base_name = storage_image_base_name(incus_image["fingerprint"], alias)
            root = storage_root_for_node(conn, node["id"])
            export_dir = f"{root}/incus-images/{base_name}"
            ts = now_ts()
            row = conn.execute(
                """
                INSERT INTO storage_image_files (
                    source_node_id, owner_id, fingerprint, aliases, description, architecture,
                    export_dir, base_name, size_bytes, status, last_error, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 'pending', '', %s, %s)
                ON CONFLICT (source_node_id, fingerprint) DO UPDATE SET
                    owner_id = COALESCE(storage_image_files.owner_id, EXCLUDED.owner_id),
                    aliases = EXCLUDED.aliases,
                    description = EXCLUDED.description,
                    architecture = EXCLUDED.architecture,
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
                    incus_image["fingerprint"],
                    incus_image["aliases"],
                    incus_image["description"],
                    incus_image["architecture"],
                    export_dir,
                    base_name,
                    ts,
                    ts,
                ),
            ).fetchone()
            task = enqueue_node_task(
                conn,
                node["id"],
                None,
                "incus_image_export",
                {
                    "storage_image_file_id": row["id"],
                    "image_ref": image_ref,
                    "alias": alias,
                    "export_dir": export_dir,
                    "base_name": base_name,
                },
            )
            audit(conn, "admin", "export", f"storage-image:{row['id']}", {"node": node["hostname"], "image_ref": image_ref})
            return {
                "image": public_storage_image_file({**row, "source_node": node["hostname"], "source_node_status": node["status"], "owner": actor["username"]}),
                "task": public_task(task),
            }

    @app.delete("/api/storage/images/{image_file_id}", status_code=204)
    def remove_storage_image(image_file_id: int):
        require_admin()
        with db() as conn:
            actor = current_user(conn)
            image_file = conn.execute("SELECT * FROM storage_image_files WHERE id = %s", (image_file_id,)).fetchone()
            if not image_file:
                raise HTTPException(status_code=404, detail="自建镜像不存在")
            # 检查是否有 images 目录中的镜像引用了同一个 fingerprint
            platform_image = conn.execute(
                "SELECT id FROM images WHERE incus_ref = %s", (image_file["fingerprint"],)
            ).fetchone()
            if platform_image:
                # 只删除 storage_image_files 记录，保留平台目录中的镜像
                pass
            # 下发节点任务：清理节点上的导出文件
            source_node = conn.execute("SELECT * FROM nodes WHERE id = %s", (image_file["source_node_id"],)).fetchone()
            if source_node and source_node["status"] == "online":
                enqueue_node_task(
                    conn,
                    source_node["id"],
                    None,
                    "incus_image_cleanup",
                    {
                        "storage_image_file_id": image_file_id,
                        "export_dir": image_file["export_dir"],
                        "base_name": image_file["base_name"],
                        "fingerprint": image_file["fingerprint"],
                    },
                )
            conn.execute("DELETE FROM storage_image_files WHERE id = %s", (image_file_id,))
            audit(conn, actor["username"], "remove", f"storage-image:{image_file_id}",
                  {"fingerprint": image_file["fingerprint"], "aliases": image_file["aliases"]})

    @app.post("/api/storage/images/{image_file_id}/distribute", status_code=202)
    def distribute_storage_image(image_file_id: int, payload: StorageImageDistributeInput):
        require_admin()
        with db() as conn:
            current_user(conn)
            image_file = conn.execute("SELECT * FROM storage_image_files WHERE id = %s", (image_file_id,)).fetchone()
            if not image_file:
                raise HTTPException(status_code=404, detail="仓库镜像不存在")
            if image_file["status"] != "exported":
                raise HTTPException(status_code=400, detail="镜像尚未导出成功，不能分发")
            source_node = conn.execute("SELECT * FROM nodes WHERE id = %s", (image_file["source_node_id"],)).fetchone()
            if not source_node:
                raise HTTPException(status_code=400, detail="源节点不存在")
            filters = ["status = 'online'", "incus_status NOT IN ('unavailable', '')", "node_type IN ('compute', 'mixed')"]
            params: list[Any] = []
            if payload.target_node_ids:
                filters.append("id = ANY(%s)")
                params.append([node_id for node_id in payload.target_node_ids if node_id > 0])
            rows = conn.execute(
                f"SELECT * FROM nodes WHERE {' AND '.join(filters)} ORDER BY hostname",
                params,
            ).fetchall()
            tasks = []
            aliases = [alias.strip() for alias in image_file["aliases"].split(",") if alias.strip()]
            alias = aliases[0] if aliases else image_file["fingerprint"][:16]
            for node in rows:
                if node["id"] == image_file["source_node_id"]:
                    continue
                if node_has_incus_image(conn, node["id"], alias) or node_has_incus_image(conn, node["id"], image_file["fingerprint"]):
                    continue
                target_root = storage_root_for_node(conn, node["id"])
                _rcb = str(node.get("resource_cache_base") or "").strip()
                if _rcb:
                    target_root = _rcb.rstrip("/")
                task = enqueue_node_task(
                    conn,
                    node["id"],
                    None,
                    "incus_image_import",
                    incus_image_import_payload(
                        image_file,
                        source_node,
                        node,
                        f"{target_root}/incus-images/import-cache/{image_file['base_name']}",
                        alias,
                    ),
                )
                tasks.append(public_task(task))
            audit(conn, "admin", "distribute", f"storage-image:{image_file_id}", {"task_count": len(tasks)})
            return {"tasks": tasks}

    @app.get("/api/storage/resource-cache")
    def list_resource_cache():
        """管理员查询全量节点资源缓存状态矩阵。"""
        require_admin()
        with db() as conn:
            rows = conn.execute(
                """
                SELECT nrc.node_id, nrc.resource_id, nrc.status, nrc.local_path,
                       nrc.synced_at, nrc.size_bytes, nrc.error, nrc.updated_at,
                       n.hostname, n.status AS node_status,
                       sr.name AS resource_name, sr.resource_type, sr.version
                FROM node_resource_cache nrc
                JOIN nodes n ON n.id = nrc.node_id
                JOIN shared_resources sr ON sr.id = nrc.resource_id
                ORDER BY n.hostname, sr.resource_type, sr.name
                """
            ).fetchall()
            return rows

    @app.post("/api/storage/resources/{resource_id}/sync-to-node/{node_id}", status_code=202)
    def trigger_resource_sync(resource_id: int, node_id: int):
        """管理员手动触发将指定资源同步到指定节点。"""
        if enqueue_resource_sync_task is None:
            raise HTTPException(status_code=503, detail="同步服务未配置")
        require_admin()
        with db() as conn:
            task = enqueue_resource_sync_task(conn, node_id, resource_id)
            if task is None:
                raise HTTPException(status_code=409, detail="同步任务已在进行中，或节点/资源不可用")
            return public_task(task)

    @app.post("/api/storage/resources/{resource_id}/sync-to-all-nodes", status_code=202)
    def trigger_resource_sync_all_nodes(resource_id: int):
        """管理员手动触发将指定资源同步到所有在线可调度计算节点。"""
        if enqueue_resource_sync_task is None:
            raise HTTPException(status_code=503, detail="同步服务未配置")
        require_admin()
        with db() as conn:
            resource = conn.execute(
                "SELECT id FROM shared_resources WHERE id = %s AND enabled = TRUE",
                (resource_id,),
            ).fetchone()
            if not resource:
                raise HTTPException(status_code=404, detail="公开数据集/模型不存在")

            nodes = conn.execute(
                """
                SELECT id FROM nodes
                WHERE status = 'online'
                  AND schedulable = TRUE
                  AND maintenance = FALSE
                  AND node_type IN ('compute', 'mixed')
                ORDER BY id
                """
            ).fetchall()
            if not nodes:
                raise HTTPException(status_code=400, detail="没有在线可调度的计算节点")

            tasks = []
            for row in nodes:
                task = enqueue_resource_sync_task(conn, row["id"], resource_id)
                if task is not None:
                    tasks.append(public_task(task))
            if not tasks:
                raise HTTPException(status_code=409, detail="所有节点都已有进行中的同步任务，或节点/资源不可用")
            return {"tasks": tasks, "node_count": len(tasks)}

    @app.delete("/api/storage/resource-cache/{node_id}/{resource_id}", status_code=204)
    def clear_resource_cache(node_id: int, resource_id: int):
        """管理员清除节点本地缓存记录（不删除节点上的实际文件）。"""
        require_admin()
        with db() as conn:
            deleted = conn.execute(
                "DELETE FROM node_resource_cache WHERE node_id = %s AND resource_id = %s RETURNING id",
                (node_id, resource_id),
            ).fetchone()
            if not deleted:
                raise HTTPException(status_code=404, detail="缓存记录不存在")
