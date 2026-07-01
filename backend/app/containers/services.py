from typing import Any
from fastapi import HTTPException

from ..config import RESOURCE_CONTAINER_STATUSES
from .ports import list_container_ports, managed_ssh_keys


def validate_mount_path(path: str) -> str:
    value = path.strip().rstrip("/") or "/"
    if not value.startswith("/") or "/../" in value or value.endswith("/.."):
        raise HTTPException(status_code=400, detail="容器资源挂载路径不合法")
    return value


def list_containers(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.*, u.username AS owner, n.hostname AS node, n.ip AS node_ip, i.name AS image_name
        FROM containers c
        JOIN users u ON u.id = c.owner_id
        JOIN nodes n ON n.id = c.node_id
        JOIN images i ON i.id = c.image_id
        ORDER BY c.created_at DESC
        """
    ).fetchall()
    for container in rows:
        container["gpus"] = conn.execute(
            """
            SELECT g.id, g.slot, g.uuid, g.model, g.pci_address, g.vram_gb
            FROM container_gpus cg JOIN gpus g ON g.id = cg.gpu_id
            WHERE cg.container_id = %s
            ORDER BY g.slot
            """,
            (container["id"],),
        ).fetchall()
        container["ports"] = list_container_ports(conn, container["id"])
    return rows

def incus_create_payload(
    container: dict[str, Any],
    image: dict[str, Any],
    node: dict[str, Any],
    selected_gpus: list[dict[str, Any]],
    ports: list[dict[str, Any]],
    workspace_volume_name: str = "",
    workspace_volume_gb: int = 0,
) -> dict[str, Any]:
    return {
        "container_id": container["id"],
        "name": container["name"],
        "image": image["incus_ref"] or image["id"],
        "cpu_cores": container["cpu_cores"],
        "memory_gb": container["memory_gb"],
        "disk_gb": container["disk_gb"],
        "ssh_username": container["ssh_username"],
        # Keep the combined value for rolling upgrades of older node agents.
        "ssh_key": managed_ssh_keys(container["ssh_key"]),
        "mounts": container["mounts"],
        "gpus": [
            {
                "slot": gpu["slot"],
                "uuid": gpu["uuid"],
                "model": gpu["model"],
                "pci_address": gpu["pci_address"],
            }
            for gpu in selected_gpus
        ],
        "ports": [
            {
                "id": port["id"],
                "name": port["name"],
                "protocol": port["protocol"],
                "container_port": port["container_port"],
                "host_port": port["host_port"],
                "node_port": port["node_port"],
            }
            for port in ports
        ],
        "node": {
            "id": node["id"],
            "hostname": node["hostname"],
        },
        "workspace_volume_name": workspace_volume_name,
        "workspace_volume_gb": workspace_volume_gb,
    }

def incus_lifecycle_payload(container: dict[str, Any], operation: str) -> dict[str, Any]:
    return {
        "container_id": container["id"],
        "name": container["name"],
        "operation": operation,
        "previous_status": container["status"],
    }

def usage_for_user(conn, user_id: int) -> dict[str, int]:
    usage = conn.execute(
        """
        SELECT COALESCE(SUM(cpu_cores), 0) AS cpu_cores,
               COALESCE(SUM(memory_gb), 0) AS memory_gb,
               COALESCE(SUM(disk_gb), 0) AS disk_gb,
               COUNT(*) AS container_count
        FROM containers
        WHERE owner_id = %s
          AND status = ANY(%s::text[])
        """,
        (user_id, list(RESOURCE_CONTAINER_STATUSES)),
    ).fetchone()
    gpu_count = conn.execute(
        """
        SELECT COUNT(*) AS count FROM container_gpus cg
        JOIN containers c ON c.id = cg.container_id
        WHERE c.owner_id = %s
          AND c.status = ANY(%s::text[])
        """,
        (user_id, list(RESOURCE_CONTAINER_STATUSES)),
    ).fetchone()["count"]
    usage["gpu_count"] = gpu_count
    return usage

def build_data_mounts(
    conn,
    user: dict[str, Any],
    ssh_username: str,
    target_node_id: int | None = None,
    selected_resources: list[Any] | None = None,
) -> list[str]:
    mounts = []
    if selected_resources:
        requested_ids = [item.resource_id for item in selected_resources or []]
        rows = conn.execute(
            """
            SELECT * FROM shared_resources
            WHERE enabled = TRUE
              AND id = ANY(%s::bigint[])
            ORDER BY resource_type, name
            """,
            (requested_ids,),
        ).fetchall()
        for resource in rows:
            selection = next((item for item in selected_resources or [] if item.resource_id == resource["id"]), None)
            default_mount = resource["mount_path"]
            mount_path = validate_mount_path(selection.mount_path) if selection and selection.mount_path else default_mount
            suffix = ":ro" if resource["readonly"] else ""
            mounts.append(f"{resource['source_path']}:{mount_path}{suffix}")
    return mounts
