import re
from typing import Any

from fastapi import HTTPException

from ..config import RESOURCE_CONTAINER_STATUSES, STALE_AFTER_SECONDS
from ..core import audit, hash_token, now_ts
from ..schemas import ContainerStateReport, IncusImageReport, NodeRegistration, StorageVolumeReport

def public_node(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    row.pop("node_token", None)
    return row

def allowed_node_ids_for_user(conn, user: dict[str, Any]) -> set[int] | None:
    user_rows = conn.execute(
        "SELECT node_id FROM user_node_access WHERE user_id = %s ORDER BY node_id",
        (user["id"],),
    ).fetchall()
    if user_rows:
        return {row["node_id"] for row in user_rows}
    group_rows = conn.execute(
        "SELECT node_id FROM quota_profile_node_access WHERE group_name = %s ORDER BY node_id",
        (user.get("group_name") or "member",),
    ).fetchall()
    if group_rows:
        return {row["node_id"] for row in group_rows}
    return None

def node_allowed_for_user(conn, user: dict[str, Any], node_id: int) -> bool:
    allowed_ids = allowed_node_ids_for_user(conn, user)
    return allowed_ids is None or node_id in allowed_ids

def mark_stale_nodes(conn):
    conn.execute(
        """
        UPDATE nodes SET status = 'offline'
        WHERE last_seen < %s AND status = 'online' AND agent_version != 'demo-seed'
        """,
        (now_ts() - STALE_AFTER_SECONDS,),
    )

def resolve_node_token(conn, payload: NodeRegistration, existing_node: dict[str, Any] | None) -> tuple[str, str]:
    token_hash = hash_token(payload.token)
    if existing_node:
        if existing_node["node_token"] != token_hash:
            raise HTTPException(status_code=403, detail="节点 token 与已注册节点不匹配")
        return token_hash, existing_node["node_group"]
    join_token = conn.execute(
        """
        SELECT * FROM node_join_tokens
        WHERE token_hash = %s AND status = 'pending' AND expires_at >= %s
        """,
        (token_hash, now_ts()),
    ).fetchone()
    if not join_token:
        raise HTTPException(status_code=403, detail="节点注册 token 无效、已使用或已过期")
    if join_token["expected_hostname"] and join_token["expected_hostname"] != payload.hostname:
        raise HTTPException(status_code=403, detail="节点注册 token 与 hostname 不匹配")
    return token_hash, join_token["node_group"]

def upsert_node(conn, payload: NodeRegistration, actor: str = "node-agent", trusted: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{1,62}", payload.hostname):
        raise HTTPException(status_code=400, detail="hostname 不合法")
    ts = now_ts()
    existing = conn.execute("SELECT * FROM nodes WHERE hostname = %s", (payload.hostname,)).fetchone()
    if trusted:
        token_hash = hash_token(payload.token)
        token_node_group = payload.node_group
    else:
        token_hash, token_node_group = resolve_node_token(conn, payload, existing)
    node_group = payload.node_group if payload.node_group != "unassigned" else token_node_group
    values = (
        payload.ip,
        node_group,
        payload.driver_pool,
        "online",
        payload.resources.cpu_model.strip()[:200],
        payload.resources.cpu_total,
        payload.resources.memory_total_gb,
        payload.resources.disk_total_gb,
        payload.resources.cpu_used,
        payload.resources.memory_used_gb,
        payload.resources.disk_used_gb,
        ts,
        payload.resources.load_avg,
        payload.resources.cpu_usage_percent,
        payload.resources.swap_total_gb,
        payload.resources.swap_used_gb,
        payload.os_version,
        payload.kernel_version,
        payload.driver_version,
        payload.cuda_driver_api_version,
        payload.incus_status,
        payload.agent_version,
        payload.uptime_seconds,
        token_hash,
        payload.resources.cpu_cores,
        payload.resources.cpu_sockets,
        payload.resources.cpu_temperature_c,
    )
    if existing:
        node_id = existing["id"]
        conn.execute(
            """
            UPDATE nodes SET ip = %s, node_group = %s, driver_pool = %s, status = %s,
                cpu_model = %s, cpu_total = %s, memory_total_gb = %s, disk_total_gb = %s, cpu_used = %s,
                memory_used_gb = %s, disk_used_gb = %s, last_seen = %s, load_avg = %s,
                cpu_usage_percent = %s, swap_total_gb = %s, swap_used_gb = %s,
                os_version = %s, kernel_version = %s, driver_version = %s,
                cuda_driver_api_version = %s, incus_status = %s, agent_version = %s,
                uptime_seconds = %s,
                node_token = %s,
                cpu_cores = %s, cpu_sockets = %s, cpu_temperature_c = %s
            WHERE id = %s
            """,
            (*values, node_id),
        )
        action = "heartbeat"
    else:
        node_id = conn.execute(
            """
            INSERT INTO nodes (
                hostname, ip, node_group, driver_pool, status, cpu_model, cpu_total, memory_total_gb,
                disk_total_gb, cpu_used, memory_used_gb, disk_used_gb, last_seen, load_avg,
                cpu_usage_percent, swap_total_gb, swap_used_gb,
                os_version, kernel_version, driver_version, cuda_driver_api_version,
                incus_status, agent_version, uptime_seconds, node_token, registered_at,
                cpu_cores, cpu_sockets, cpu_temperature_c
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (payload.hostname, *values, ts),
        ).fetchone()["id"]
        action = "register"
        if not trusted:
            conn.execute(
                """
                UPDATE node_join_tokens
                SET status = 'used', node_id = %s, used_at = %s
                WHERE token_hash = %s
                """,
                (node_id, ts, token_hash),
            )
    seen_gpu_ids: list[int] = []
    for gpu in payload.gpus:
        existing_gpu = conn.execute("SELECT id FROM gpus WHERE uuid = %s", (gpu.uuid,)).fetchone()
        values = (node_id, gpu.slot, gpu.model, gpu.pci_address, gpu.vram_gb, gpu.vram_used_mb, gpu.temperature_c, gpu.power_w, gpu.utilization)
        if existing_gpu:
            conn.execute(
                """
                UPDATE gpus SET node_id = %s, slot = %s, model = %s, pci_address = %s, vram_gb = %s,
                    vram_used_mb = %s, temperature_c = %s, power_w = %s, utilization = %s
                WHERE id = %s
                """,
                (*values, existing_gpu["id"]),
            )
            seen_gpu_ids.append(existing_gpu["id"])
        else:
            gpu_id = conn.execute(
                """
                INSERT INTO gpus (node_id, slot, uuid, model, pci_address, vram_gb, vram_used_mb, temperature_c, power_w, utilization)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (node_id, gpu.slot, gpu.uuid, gpu.model, gpu.pci_address, gpu.vram_gb, gpu.vram_used_mb, gpu.temperature_c, gpu.power_w, gpu.utilization),
            ).fetchone()["id"]
            seen_gpu_ids.append(gpu_id)
    if seen_gpu_ids:
        conn.execute(
            """
            DELETE FROM gpus
            WHERE node_id = %s AND NOT (id = ANY(%s)) AND id NOT IN (SELECT gpu_id FROM container_gpus)
            """,
            (node_id, seen_gpu_ids),
        )
    else:
        conn.execute(
            """
            DELETE FROM gpus
            WHERE node_id = %s AND id NOT IN (SELECT gpu_id FROM container_gpus)
            """,
            (node_id,),
    )
    audit(conn, actor, action, f"node:{node_id}", {"hostname": payload.hostname, "gpu_count": len(payload.gpus)})
    sync_node_containers(conn, node_id, payload.containers or [], payload.incus_status)
    sync_node_images(conn, node_id, payload.images or [], payload.incus_status)
    sync_storage_volumes(conn, node_id, payload.storage_volumes or [])
    return get_node(conn, node_id)

def normalize_reported_container_status(status: str) -> str:
    value = status.strip().lower()
    if value == "running":
        return "running"
    if value in ("stopped", "stopping", "frozen", "error"):
        return "stopped"
    return ""

def sync_node_containers(conn, node_id: int, reports: list[ContainerStateReport], incus_status: str):
    if incus_status in ("unavailable", ""):
        return
    transitional = ("provisioning", "starting", "stopping", "restarting", "deleting")
    by_name = {report.name: report for report in reports if report.name}
    ts = now_ts()
    for report in reports:
        if report.role != "resource_downloader" or not report.name:
            continue
        status = normalize_reported_container_status(report.status) or "stopped"
        admin = conn.execute(
            "SELECT id FROM users WHERE username = 'admin' ORDER BY id LIMIT 1",
        ).fetchone()
        if not admin:
            continue
        conn.execute(
            """
            INSERT INTO containers (
                name, owner_id, node_id, image_id, status, cpu_cores, memory_gb, disk_gb,
                ssh_username, ssh_key, mounts, ip, access_status, access_error, system_role,
                created_at, updated_at
            ) VALUES (%s, %s, %s, 'system/resource-downloader', %s, 1, 2, 20,
                      'root', '', '[]', %s, 'ready', '', 'resource_downloader', %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                node_id = EXCLUDED.node_id,
                status = EXCLUDED.status,
                ip = EXCLUDED.ip,
                access_status = 'ready',
                access_error = '',
                system_role = 'resource_downloader',
                updated_at = EXCLUDED.updated_at
            """,
            (report.name, admin["id"], node_id, status, report.ip if status == "running" else "", ts, ts),
        )
    rows = conn.execute(
        "SELECT id, name, status, ip FROM containers WHERE node_id = %s ORDER BY id",
        (node_id,),
    ).fetchall()
    for container in rows:
        if container["status"] in transitional or container["status"] == "failed":
            continue
        report = by_name.get(container["name"])
        if not report:
            if container["status"] not in ("missing", "failed"):
                conn.execute(
                    "UPDATE containers SET status = 'missing', ip = '', updated_at = %s WHERE id = %s",
                    (ts, container["id"]),
                )
            continue
        next_status = normalize_reported_container_status(report.status)
        if not next_status:
            continue
        next_ip = report.ip if next_status == "running" else ""
        if container["status"] != next_status or container["ip"] != next_ip:
            conn.execute(
                "UPDATE containers SET status = %s, ip = %s, updated_at = %s WHERE id = %s",
                (next_status, next_ip, ts, container["id"]),
            )

def sync_node_images(conn, node_id: int, reports: list[IncusImageReport], incus_status: str):
    if incus_status in ("unavailable", ""):
        return
    ts = now_ts()
    seen: list[str] = []
    for report in reports:
        fingerprint = report.fingerprint.strip()
        if not fingerprint:
            continue
        seen.append(fingerprint)
        conn.execute(
            """
            INSERT INTO node_incus_images (node_id, fingerprint, aliases, description, architecture, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (node_id, fingerprint) DO UPDATE SET
                aliases = EXCLUDED.aliases,
                description = EXCLUDED.description,
                architecture = EXCLUDED.architecture,
                updated_at = EXCLUDED.updated_at
            """,
            (node_id, fingerprint, report.aliases, report.description, report.architecture, ts),
        )
    if seen:
        conn.execute(
            "DELETE FROM node_incus_images WHERE node_id = %s AND NOT (fingerprint = ANY(%s))",
            (node_id, seen),
        )
    else:
        conn.execute("DELETE FROM node_incus_images WHERE node_id = %s", (node_id,))

def normalize_storage_volume_status(value: str, exists: bool) -> str:
    status = value.strip().lower()
    if status in {"ok", "warning", "missing", "error", "unknown"}:
        return status
    return "ok" if exists else "missing"

def sync_storage_volumes(conn, node_id: int, reports: list[StorageVolumeReport]):
    ts = now_ts()
    seen: list[str] = []
    for report in reports:
        name = report.name.strip().lower()
        if name not in {"root", "users", "datasets", "models", "backups"}:
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,40}", name):
            continue
        path = report.path.strip()[:240]
        seen.append(name)
        conn.execute(
            """
            INSERT INTO storage_volume_reports (
                node_id, volume_name, path, exists, total_gb, used_gb, free_gb,
                directory_used_gb, status, error, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (node_id, volume_name) DO UPDATE SET
                path = EXCLUDED.path,
                exists = EXCLUDED.exists,
                total_gb = EXCLUDED.total_gb,
                used_gb = EXCLUDED.used_gb,
                free_gb = EXCLUDED.free_gb,
                directory_used_gb = EXCLUDED.directory_used_gb,
                status = EXCLUDED.status,
                error = EXCLUDED.error,
                updated_at = EXCLUDED.updated_at
            """,
            (
                node_id,
                name,
                path,
                report.exists,
                max(0, report.total_gb),
                max(0, report.used_gb),
                max(0, report.free_gb),
                max(0, report.directory_used_gb),
                normalize_storage_volume_status(report.status, report.exists),
                report.error.strip()[:1000],
                ts,
            ),
        )

def get_node(conn, node_id: int) -> dict[str, Any]:
    node = public_node(conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone())
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    node["gpus"] = [
        gpu_with_container(conn, gpu)
        for gpu in conn.execute("SELECT * FROM gpus WHERE node_id = %s ORDER BY slot", (node_id,)).fetchall()
    ]
    return node

def gpu_with_container(conn, gpu: dict[str, Any]) -> dict[str, Any]:
    containers = conn.execute(
        """
        SELECT c.id, c.name, c.status, u.username AS owner
        FROM container_gpus cg
        JOIN containers c ON c.id = cg.container_id
        JOIN users u ON u.id = c.owner_id
        WHERE cg.gpu_id = %s
          AND c.status = ANY(%s::text[])
        """,
        (gpu["id"], list(RESOURCE_CONTAINER_STATUSES)),
    ).fetchall()
    gpu["containers"] = containers
    gpu["container"] = containers[0] if containers else None
    return gpu
