import os
from typing import Any

import re

from fastapi import HTTPException

from ..config import AGENT_RELEASE_DIR, SYNC_SSH_IDENTITY_FILE, SYNC_SSH_PORT, SYNC_SSH_USER


def _read_sync_private_key() -> str:
    """读取集群同步 SSH 私钥内容。优先 SYNC_SSH_IDENTITY_FILE，次选 agent 自管理密钥。"""
    candidates = []
    if SYNC_SSH_IDENTITY_FILE:
        candidates.append(SYNC_SSH_IDENTITY_FILE)
    candidates.append(os.path.join(AGENT_RELEASE_DIR, ".cluster_node_key"))
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return f.read()
            except OSError:
                continue
    return ""


def is_remote_incus_ref(image_ref: str) -> bool:
    if ":" not in image_ref:
        return False
    remote, _, value = image_ref.partition(":")
    return bool(remote and value and "/" in value)


def node_has_incus_image(conn, node_id: int, image_ref: str) -> bool:
    image_ref = image_ref.strip()
    if not image_ref or is_remote_incus_ref(image_ref):
        return True
    rows = conn.execute(
        """
        SELECT fingerprint, aliases, description
        FROM node_incus_images
        WHERE node_id = %s
        """,
        (node_id,),
    ).fetchall()
    for row in rows:
        fingerprint = row["fingerprint"].strip()
        if fingerprint and (fingerprint.startswith(image_ref) or image_ref.startswith(fingerprint)):
            return True
        aliases = [alias.strip() for alias in row["aliases"].split(",") if alias.strip()]
        if image_ref in aliases:
            return True
        if image_ref == row["description"].strip():
            return True
    return False


def incus_image_ref_matches(row: dict[str, Any], image_ref: str) -> bool:
    image_ref = image_ref.strip()
    if not image_ref:
        return False
    fingerprint = row["fingerprint"].strip()
    if fingerprint and (fingerprint.startswith(image_ref) or image_ref.startswith(fingerprint)):
        return True
    aliases = [alias.strip() for alias in row.get("aliases", "").split(",") if alias.strip()]
    if image_ref in aliases:
        return True
    return image_ref == row.get("description", "").strip()


def find_node_incus_image(conn, node_id: int, image_ref: str) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT *
        FROM node_incus_images
        WHERE node_id = %s
        ORDER BY aliases, description, fingerprint
        """,
        (node_id,),
    ).fetchall()
    for row in rows:
        if incus_image_ref_matches(row, image_ref):
            return row
    return None


def storage_root_for_node(conn, node_id: int) -> str:
    row = conn.execute(
        """
        SELECT path
        FROM storage_volume_reports
        WHERE node_id = %s AND volume_name = 'root' AND exists = TRUE
        """,
        (node_id,),
    ).fetchone()
    if not row:
        return "/data"
    path = row["path"].strip()
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="存储根目录 必须是绝对路径")
    if "\x00" in path or "/../" in path or path.endswith("/.."):
        raise HTTPException(status_code=400, detail="存储根目录 不合法")
    if len(path) > 240:
        raise HTTPException(status_code=400, detail="存储根目录 过长")
    return path.rstrip("/") or "/"


def source_path_for_node(platform_path: str, node: dict[str, Any]) -> str:
    path = platform_path.strip()
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="平台路径必须是绝对路径")
    storage_root = str(node.get("storage_root") or storage_root_for_node_from_row(node) or "/data").strip() or "/data"
    storage_root = storage_root.rstrip("/") or "/"
    if path == storage_root or path.startswith(storage_root + "/"):
        return path
    if path == "/data":
        return storage_root
    if path.startswith("/data/"):
        return f"{storage_root}/{path[len('/data/'):]}".replace("//", "/")
    return path


def storage_root_for_node_from_row(node: dict[str, Any]) -> str:
    root = str(node.get("storage_root") or "").strip()
    if root.startswith("/"):
        return root.rstrip("/") or "/"
    return ""


def select_storage_node_for_path(conn, path: str) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT n.*, svr.path AS storage_root
        FROM nodes n
        LEFT JOIN storage_volume_reports svr ON svr.node_id = n.id AND svr.volume_name = 'root'
        WHERE n.status = 'online'
          AND n.node_type IN ('storage', 'mixed')
        ORDER BY
          CASE
            WHEN svr.exists = TRUE AND %s LIKE svr.path || '%%' THEN 1
            WHEN svr.exists = TRUE THEN 2
            ELSE 3
          END,
          CASE n.node_type WHEN 'storage' THEN 1 ELSE 2 END,
          n.hostname
        LIMIT 1
        """,
        (path,),
    ).fetchone()


def ensure_user_zfs_dataset_task(
    conn,
    user_id: int,
    now_ts,
    enqueue_node_task,
    *,
    reason: str = "ensure",
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT u.id, u.username, udp.home_path, COALESCE(q.storage_quota_gb, 0) AS storage_quota_gb
        FROM users u
        JOIN user_data_policies udp ON udp.user_id = u.id
        LEFT JOIN quotas q ON q.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="用户或用户目录策略不存在")
    node = select_storage_node_for_path(conn, row["home_path"])
    ts = now_ts()
    if not node:
        conn.execute(
            """
            INSERT INTO user_storage_datasets (
                user_id, node_id, dataset_name, mountpoint, quota_gb,
                status, last_error, created_at, updated_at
            ) VALUES (%s, NULL, '', %s, %s, 'failed', %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                node_id = NULL,
                mountpoint = EXCLUDED.mountpoint,
                quota_gb = EXCLUDED.quota_gb,
                status = 'failed',
                last_error = EXCLUDED.last_error,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, row["home_path"], int(row["storage_quota_gb"] or 0), "没有 online 的 storage/mixed 节点可创建 ZFS dataset", ts, ts),
        )
        return None
    mountpoint = source_path_for_node(row["home_path"], node)
    conn.execute(
        """
        INSERT INTO user_storage_datasets (
            user_id, node_id, dataset_name, mountpoint, quota_gb,
            status, last_error, created_at, updated_at
        ) VALUES (%s, %s, '', %s, %s, 'pending', '', %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            node_id = EXCLUDED.node_id,
            mountpoint = EXCLUDED.mountpoint,
            quota_gb = EXCLUDED.quota_gb,
            status = 'pending',
            last_error = '',
            updated_at = EXCLUDED.updated_at
        """,
        (user_id, node["id"], mountpoint, int(row["storage_quota_gb"] or 0), ts, ts),
    )
    return enqueue_node_task(
        conn,
        node["id"],
        None,
        "ensure_user_zfs_dataset",
        {
            "user_id": user_id,
            "username": row["username"],
            "platform_home_path": row["home_path"],
            "mountpoint": mountpoint,
            "quota_gb": int(row["storage_quota_gb"] or 0),
            "dataset_name": "",
            "uid": 0,
            "gid": 0,
            "mode": "0750",
            "reason": reason,
        },
    )


def remove_user_zfs_dataset_task(
    conn,
    user_id: int,
    now_ts,
    enqueue_node_task,
) -> dict[str, Any] | None:
    """移除用户的 ZFS dataset。只要有节点和路径，就下发 agent 到存储节点清理实际目录。"""
    row = conn.execute(
        """
        SELECT usd.*, n.status AS node_status
        FROM user_storage_datasets usd
        LEFT JOIN nodes n ON n.id = usd.node_id
        WHERE usd.user_id = %s
        """,
        (user_id,),
    ).fetchone()
    ts = now_ts()
    if not row:
        user = conn.execute(
            """
            SELECT u.id, udp.home_path
            FROM users u
            LEFT JOIN user_data_policies udp ON udp.user_id = u.id
            WHERE u.id = %s
            """,
            (user_id,),
        ).fetchone()
        home_path = str(user["home_path"] if user else "").strip()
        if not home_path:
            return None
        node = select_storage_node_for_path(conn, home_path)
        if not node:
            return None
        mountpoint = source_path_for_node(home_path, node)
        conn.execute(
            """
            INSERT INTO user_storage_datasets (
                user_id, node_id, dataset_name, mountpoint, quota_gb,
                status, last_error, created_at, updated_at
            ) VALUES (%s, %s, '', %s, 0, 'removing', '', %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                node_id = EXCLUDED.node_id,
                mountpoint = EXCLUDED.mountpoint,
                status = 'removing',
                last_error = '',
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, node["id"], mountpoint, ts, ts),
        )
        return enqueue_node_task(
            conn,
            node["id"],
            None,
            "remove_user_zfs_dataset",
            {
                "user_id": user_id,
                "dataset_name": "",
                "mountpoint": mountpoint,
            },
        )
    dataset_name = str(row["dataset_name"] or "").strip()
    mountpoint = str(row["mountpoint"] or "").strip()
    if not dataset_name and not mountpoint:
        conn.execute("DELETE FROM user_storage_datasets WHERE user_id = %s", (user_id,))
        return None
    if not row["node_id"] or row["node_status"] != "online":
        conn.execute(
            """
            UPDATE user_storage_datasets
            SET status = 'failed', last_error = %s, updated_at = %s
            WHERE user_id = %s
            """,
            ("没有 online 的节点可移除 ZFS dataset", now_ts(), user_id),
        )
        return None
    conn.execute(
        """
        UPDATE user_storage_datasets
        SET status = 'removing', last_error = '', updated_at = %s
        WHERE user_id = %s
        """,
        (ts, user_id),
    )
    return enqueue_node_task(
        conn,
        row["node_id"],
        None,
        "remove_user_zfs_dataset",
        {
            "user_id": user_id,
            "dataset_name": dataset_name,
            "mountpoint": mountpoint,
        },
    )


def remove_user_workspace_volume_task(
    conn,
    user_id: int,
    node_id: int,
    now_ts,
    enqueue_node_task,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT uwv.*, n.status AS node_status
        FROM user_workspace_volumes uwv
        JOIN nodes n ON n.id = uwv.node_id
        WHERE uwv.user_id = %s AND uwv.node_id = %s
        """,
        (user_id, node_id),
    ).fetchone()
    if not row or row["status"] == "removed":
        raise HTTPException(status_code=404, detail="用户节点数据卷不存在")
    active_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM containers
        WHERE owner_id = %s AND node_id = %s AND status != 'deleting'
        """,
        (user_id, node_id),
    ).fetchone()["count"]
    if active_count:
        raise HTTPException(status_code=400, detail="该用户在当前节点仍有容器，不能回收数据卷")
    if row["node_status"] != "online":
        raise HTTPException(status_code=400, detail="数据卷所在节点不在线")
    ts = now_ts()
    conn.execute(
        """
        UPDATE user_workspace_volumes
        SET status = 'removing', last_error = '', updated_at = %s
        WHERE user_id = %s AND node_id = %s
        """,
        (ts, user_id, node_id),
    )
    return enqueue_node_task(
        conn,
        node_id,
        None,
        "remove_user_workspace_volume",
        {
            "user_id": user_id,
            "node_id": node_id,
            "volume_name": row["volume_name"],
        },
    )


def storage_image_base_name(fingerprint: str, alias: str) -> str:
    label = alias.strip() or fingerprint.strip()[:16]
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-_.").lower()
    if not label:
        label = "incus-image"
    suffix = fingerprint.strip()[:12]
    if suffix and suffix not in label:
        label = f"{label}-{suffix}"
    return label[:96]


def public_storage_image_file(row: dict[str, Any]) -> dict[str, Any]:
    aliases = [alias.strip() for alias in row["aliases"].split(",") if alias.strip()]
    return {
        "id": row["id"],
        "source_node_id": row["source_node_id"],
        "owner_id": row.get("owner_id"),
        "owner": row.get("owner", ""),
        "source_node": row.get("source_node", ""),
        "source_node_status": row.get("source_node_status", ""),
        "fingerprint": row["fingerprint"],
        "aliases": row["aliases"],
        "alias": aliases[0] if aliases else "",
        "description": row["description"],
        "architecture": row["architecture"],
        "export_dir": row["export_dir"],
        "base_name": row["base_name"],
        "size_bytes": row["size_bytes"],
        "status": row["status"],
        "last_error": row["last_error"],
        "exported_at": row["exported_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def select_storage_image_file(conn, image_ref: str) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT sif.*, n.hostname AS source_node, n.status AS source_node_status, n.ip AS source_node_ip
        FROM storage_image_files sif
        JOIN nodes n ON n.id = sif.source_node_id
        WHERE sif.status = 'exported'
          AND n.status = 'online'
        ORDER BY sif.exported_at DESC, sif.updated_at DESC
        """
    ).fetchall()
    for row in rows:
        if incus_image_ref_matches(row, image_ref):
            return row
    return None


def storage_image_available(conn, image_ref: str) -> bool:
    return bool(select_storage_image_file(conn, image_ref))


def incus_image_import_payload(
    image_file: dict[str, Any],
    source_node: dict[str, Any],
    target_node: dict[str, Any],
    target_path: str,
    alias: str,
) -> dict[str, Any]:
    sync_ip = str(source_node.get("sync_ip") or "").strip()
    sync_port = int(source_node.get("sync_ssh_port") or 0)
    return {
        "storage_image_file_id": image_file["id"],
        "source_node_id": image_file["source_node_id"],
        "target_node_id": target_node["id"],
        "source_path": image_file["export_dir"],
        "target_path": target_path,
        "base_name": image_file["base_name"],
        "alias": alias,
        "fingerprint": image_file["fingerprint"],
        "source_endpoint": {
            "hostname": source_node["hostname"],
            "host": sync_ip or source_node["ip"],
            "port": sync_port or SYNC_SSH_PORT,
            "user": source_node.get("ssh_user") or SYNC_SSH_USER,
            "private_key": _read_sync_private_key(),  # 传密钥内容而非路径，节点上路径可能不同
        },
    }


def incus_image_push_payload(
    source_compute_node: dict[str, Any],
    storage_node: dict[str, Any],
    compute_export_dir: str,
    storage_export_dir: str,
    base_name: str,
    storage_image_file_id: int,
    distribute_to_node_ids: list[int],
) -> dict[str, Any]:
    """构建 incus_image_push_to_storage 任务的 payload。

    Agent 将此视为 DataSyncPayload（本地 source_path → 远端 TargetEndpoint:target_path）。
    额外字段（storage_image_file_id 等）由 agent 忽略，供管理端回调使用。
    """
    sync_ip = str(storage_node.get("sync_ip") or "").strip()
    sync_port = int(storage_node.get("sync_ssh_port") or 0)
    return {
        # DataSyncPayload 字段（agent 使用）
        "source_node_id": source_compute_node["id"],
        "target_node_id": storage_node["id"],
        "source_path": compute_export_dir,
        "target_path": storage_export_dir,
        "delete": True,
        "target_endpoint": {
            "hostname": storage_node["hostname"],
            "host": sync_ip or storage_node["ip"],
            "port": sync_port or SYNC_SSH_PORT,
            "user": storage_node.get("ssh_user") or SYNC_SSH_USER,
            "private_key": _read_sync_private_key(),
        },
        # 管理端回调元数据（agent 忽略）
        "storage_image_file_id": storage_image_file_id,
        "compute_node_id": source_compute_node["id"],
        "compute_export_dir": compute_export_dir,
        "base_name": base_name,
        "distribute_to_node_ids": distribute_to_node_ids,
    }


def enqueue_incus_image_import_task(
    conn,
    node: dict[str, Any],
    container_id: int | None,
    image_ref: str,
    now_ts,
    enqueue_node_task,
) -> dict[str, Any] | None:
    if node_has_incus_image(conn, node["id"], image_ref):
        return None
    image_file = select_storage_image_file(conn, image_ref)
    if not image_file:
        return None
    source_node = conn.execute("SELECT * FROM nodes WHERE id = %s", (image_file["source_node_id"],)).fetchone()
    if not source_node:
        return None
    target_root = storage_root_for_node(conn, node["id"])
    target_path = f"{target_root}/incus-images/import-cache/{image_file['base_name']}"
    return enqueue_node_task(
        conn,
        node["id"],
        container_id,
        "incus_image_import",
        incus_image_import_payload(image_file, source_node, node, target_path, image_ref),
    )
