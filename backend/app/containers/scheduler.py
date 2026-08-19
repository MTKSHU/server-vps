from typing import Any

from ..config import RESOURCE_CONTAINER_STATUSES, RUNNING_CONTAINER_STATUSES
from ..platform_settings import effective_node_shared_storage_mode
from ..schemas import ContainerCreate
from ..storage.services import node_has_incus_image, storage_image_available

def effective_limit(value: int, fallback: int) -> int:
    return value if value > 0 else fallback

def count_node_containers(conn, node_id: int, statuses: tuple[str, ...]) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS count FROM containers WHERE node_id = %s AND status = ANY(%s::text[]) AND system_role = ''",
        (node_id, list(statuses)),
    ).fetchone()["count"]

def schedulable_gpus_for_node(conn, node: dict[str, Any], model_preference: str) -> list[dict[str, Any]]:
    query = """
        SELECT g.*, COUNT(c.id) AS active_container_count
        FROM gpus g
        LEFT JOIN container_gpus cg ON cg.gpu_id = g.id
        LEFT JOIN containers c ON c.id = cg.container_id AND c.status = ANY(%s::text[])
        WHERE g.node_id = %s
    """
    params: list[Any] = [list(RESOURCE_CONTAINER_STATUSES), node["id"]]
    if model_preference:
        query += " AND lower(g.model) LIKE %s"
        params.append(f"%{model_preference.lower()}%")
    if not node["allow_gpu_sharing"]:
        query += " GROUP BY g.id HAVING COUNT(c.id) = 0 ORDER BY g.slot"
    else:
        query += " GROUP BY g.id HAVING COUNT(c.id) < %s ORDER BY active_container_count, g.slot"
        params.append(node["max_gpu_shared_containers"])
    return conn.execute(query, params).fetchall()

def requested_gpus_for_node(conn, node: dict[str, Any], gpu_ids: list[int]) -> list[dict[str, Any]]:
    if not gpu_ids:
        return []
    rows = conn.execute(
        """
        SELECT g.*, COUNT(c.id) AS active_container_count
        FROM gpus g
        LEFT JOIN container_gpus cg ON cg.gpu_id = g.id
        LEFT JOIN containers c ON c.id = cg.container_id AND c.status = ANY(%s::text[])
        WHERE g.node_id = %s
          AND g.id = ANY(%s::bigint[])
        GROUP BY g.id
        """,
        (list(RESOURCE_CONTAINER_STATUSES), node["id"], gpu_ids),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [by_id[gpu_id] for gpu_id in gpu_ids if gpu_id in by_id]

def select_node_and_gpus(
    conn,
    image: dict[str, Any],
    payload: ContainerCreate,
    allowed_node_ids: set[int] | None = None,
    *,
    shared_storage_settings: dict[str, Any] | None = None,
    user_id: int | None = None,
):
    compatible_pools = set(image["compatible_pools"].split(","))
    candidates = []
    reasons = []
    requested_gpu_ids = list(dict.fromkeys(gpu_id for gpu_id in payload.gpu_ids if gpu_id > 0))
    node_filters = ["status = 'online'", "maintenance = FALSE", "schedulable = TRUE"]
    params: list[Any] = []
    if payload.node_id is not None:
        node_filters.append("id = %s")
        params.append(payload.node_id)
    online_nodes = conn.execute(f"SELECT * FROM nodes WHERE {' AND '.join(node_filters)}", params).fetchall()
    if not online_nodes:
        if payload.node_id is not None:
            selected = conn.execute("SELECT * FROM nodes WHERE id = %s", (payload.node_id,)).fetchone()
            if not selected:
                reasons.append("所选节点不存在")
            elif selected["maintenance"]:
                reasons.append(f"{selected['hostname']}: 节点处于维护模式")
            elif not selected["schedulable"]:
                reasons.append(f"{selected['hostname']}: 节点未参与调度")
            else:
                reasons.append(f"{selected['hostname']}: 节点状态为 {selected['status']}，不是 online")
        else:
            reasons.append("没有 online 且未维护的节点")
    for node in online_nodes:
        hostname = node["hostname"]
        if allowed_node_ids is not None and node["id"] not in allowed_node_ids:
            reasons.append(f"{hostname}: 当前用户不可使用该节点")
            continue
        if node["incus_status"] in ("unavailable", ""):
            reasons.append(f"{hostname}: Incus 不可用（状态：{node['incus_status']}）")
            continue
        if node["node_type"] == "storage":
            reasons.append(f"{hostname}: 节点类型为 storage，不创建业务容器")
            continue
        if node["driver_pool"] not in compatible_pools:
            reasons.append(
                f"{hostname}: 驱动池 {node['driver_pool']} 不兼容镜像 {image['name']}，需要 {image['compatible_pools']}"
            )
            continue
        effective_storage_mode = effective_node_shared_storage_mode(
            node, shared_storage_settings or {"shared_storage_mode": "disabled"}, user_id
        )
        capabilities = set(node.get("capabilities") or [])
        if (effective_storage_mode == "enabled" or payload.resources) and "managed_nfs_mounts_v1" not in capabilities:
            reasons.append(f"{hostname}: agent 尚不支持托管挂载")
            continue
        if effective_storage_mode == "enabled" and "per_user_nfs_exports_v1" not in capabilities:
            reasons.append(f"{hostname}: agent 尚不支持每用户独立 NFS 导出")
            continue
        unavailable_resources = []
        for selection in payload.resources:
            cache = conn.execute(
                "SELECT 1 FROM node_resource_cache WHERE node_id=%s AND resource_id=%s AND status='ready' AND local_path!=''",
                (node["id"], selection.resource_id),
            ).fetchone()
            if not cache and effective_storage_mode != "enabled":
                unavailable_resources.append(str(selection.resource_id))
        if unavailable_resources:
            reasons.append(f"{hostname}: 资源 {','.join(unavailable_resources)} 无 ready 本地缓存，且该节点 NFS 已禁用")
            continue
        image_ref = image["incus_ref"] or image["id"]
        if not node_has_incus_image(conn, node["id"], image_ref):
            if not storage_image_available(conn, image_ref):
                reasons.append(f"{hostname}: 节点 Incus 镜像库存缺少 {image_ref}，且 storage 仓库尚无可分发副本")
                continue
        container_count = count_node_containers(conn, node["id"], RESOURCE_CONTAINER_STATUSES)
        if container_count >= node["max_containers"]:
            reasons.append(f"{hostname}: 容器数量达到上限 {node['max_containers']}")
            continue
        running_count = count_node_containers(conn, node["id"], RUNNING_CONTAINER_STATUSES)
        if running_count >= node["max_running_containers"]:
            reasons.append(f"{hostname}: 运行中容器数量达到上限 {node['max_running_containers']}")
            continue
        if payload.ports and not node["allow_port_mapping"]:
            reasons.append(f"{hostname}: 节点不允许端口映射")
            continue
        if len(payload.ports) > node["max_ports_per_container"]:
            reasons.append(f"{hostname}: 单容器端口映射最多 {node['max_ports_per_container']} 个")
            continue
        max_cpu = effective_limit(node["max_cpu_per_container"], node["cpu_total"])
        max_memory = effective_limit(node["max_memory_gb_per_container"], node["memory_total_gb"])
        max_disk = effective_limit(node["max_disk_gb_per_container"], node["disk_total_gb"])
        if payload.cpu_cores > max_cpu:
            reasons.append(f"{hostname}: 单容器 CPU 上限 {max_cpu} 核，请求 {payload.cpu_cores} 核")
            continue
        if payload.memory_gb > max_memory:
            reasons.append(f"{hostname}: 单容器内存上限 {max_memory} GB，请求 {payload.memory_gb} GB")
            continue
        if payload.disk_gb > max_disk:
            reasons.append(f"{hostname}: 单容器磁盘上限 {max_disk} GB，请求 {payload.disk_gb} GB")
            continue
        available_cpu = node["cpu_total"] - node["cpu_used"]
        available_memory = node["memory_total_gb"] - node["reserved_memory_gb"] - node["memory_used_gb"]
        reserved_floor = max(float(node["disk_total_gb"]) * 0.15, 100.0)
        available_disk = node["disk_total_gb"] - max(float(node["reserved_disk_gb"]), reserved_floor) - node["disk_used_gb"]
        if payload.cpu_cores > available_cpu:
            reasons.append(f"{hostname}: CPU 上限不足，可用 {available_cpu} 核，请求 {payload.cpu_cores} 核")
            continue
        if payload.memory_gb > available_memory:
            reasons.append(f"{hostname}: 内存上限不足，可用 {available_memory} GB，请求 {payload.memory_gb} GB")
            continue
        if payload.disk_gb > available_disk:
            reasons.append(f"{hostname}: 磁盘上限不足，可用 {available_disk} GB，请求 {payload.disk_gb} GB")
            continue
        if requested_gpu_ids:
            schedulable_gpus = requested_gpus_for_node(conn, node, requested_gpu_ids)
            if len(schedulable_gpus) != len(requested_gpu_ids):
                reasons.append(f"{hostname}: 所选 GPU 不属于该节点或不存在")
                continue
            model_preference = payload.gpu_model.strip().lower()
            if model_preference and any(model_preference not in gpu["model"].lower() for gpu in schedulable_gpus):
                reasons.append(f"{hostname}: 所选 GPU 不符合型号偏好 {payload.gpu_model.strip()}")
                continue
            blocked = []
            for gpu in schedulable_gpus:
                active = int(gpu["active_container_count"] or 0)
                if not node["allow_gpu_sharing"] and active > 0:
                    blocked.append(f"GPU {gpu['slot']} 已被占用")
                elif node["allow_gpu_sharing"] and active >= node["max_gpu_shared_containers"]:
                    blocked.append(f"GPU {gpu['slot']} 共享数量达到上限 {node['max_gpu_shared_containers']}")
            if blocked:
                reasons.append(f"{hostname}: " + "，".join(blocked))
                continue
        else:
            schedulable_gpus = schedulable_gpus_for_node(conn, node, payload.gpu_model.strip())
        if len(schedulable_gpus) < payload.gpu_count:
            model_text = f"（型号匹配 {payload.gpu_model.strip()}）" if payload.gpu_model.strip() else ""
            reasons.append(f"{hostname}: GPU 不足{model_text}，可用 {len(schedulable_gpus)} 张，请求 {payload.gpu_count} 张")
            continue
        old_card_score = 20 if any("TITAN XP" in gpu["model"] for gpu in schedulable_gpus) else 0
        premium_penalty = -10 if any("4090" in gpu["model"] or "A6000" in gpu["model"] for gpu in schedulable_gpus) else 0
        gpu_share_pressure = sum(gpu["active_container_count"] for gpu in schedulable_gpus[: payload.gpu_count])
        disk_pressure = node["disk_used_gb"] / max(node["disk_total_gb"], 1)
        candidates.append(
            (
                gpu_share_pressure,
                -len(schedulable_gpus),
                -(old_card_score + premium_penalty),
                -node["scheduler_weight"],
                disk_pressure,
                hostname,
                node,
                schedulable_gpus[: payload.gpu_count],
            )
        )
    if not candidates:
        return None, [], reasons
    candidates.sort(key=lambda item: item[:6])
    return candidates[0][6], candidates[0][7], reasons
