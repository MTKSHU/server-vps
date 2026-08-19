from typing import Any
import asyncio
import time
from ..platform_settings import effective_node_shared_storage_mode, get_platform_settings


def _nfs_unavailable(node: dict, settings: dict, has_managed_mounts: bool) -> bool:
    if effective_node_shared_storage_mode(node, settings) != "enabled":
        return False
    if node["status"] != "online" or node.get("node_type") not in ("compute", "mixed"):
        return False
    if node.get("nfs_healthy"):
        return False
    return not (
        node.get("nfs_error") == "managed NFS is not mounted"
        and not has_managed_mounts
    )


def _build_alerts_standalone(conn, all_nodes: list, all_containers: list) -> list[dict]:
    """独立告警构建函数，可被 metrics routes 和 webhook 循环复用。"""
    alerts = []
    now = int(time.time())
    stale_cutoff = now - 300
    settings = get_platform_settings(conn)
    nodes_with_managed_mounts = {
        container["node_id"]
        for container in all_containers
        if container.get("managed_mounts")
    }

    for node in all_nodes:
        if node["status"] != "online":
            last_seen = node.get("last_seen") or 0
            if last_seen < stale_cutoff:
                alerts.append({"level": "error", "type": "node_offline",
                                "message": f"节点 {node['hostname']} 已离线", "node_id": node["id"]})

    for node in all_nodes:
        if node["status"] == "online" and node.get("disk_total_gb", 0) > 0:
            used_pct = node.get("disk_used_gb", 0) / node["disk_total_gb"]
            if used_pct > 0.85:
                alerts.append({"level": "warning", "type": "disk_full",
                                "message": f"节点 {node['hostname']} 磁盘使用率 {int(used_pct * 100)}%，建议清理",
                                "node_id": node["id"]})
        if _nfs_unavailable(node, settings, node["id"] in nodes_with_managed_mounts):
            alerts.append({"level": "error", "type": "nfs_unavailable",
                           "message": f"节点 {node['hostname']} 的托管 NFS 未就绪：{node.get('nfs_error') or '未知错误'}",
                           "node_id": node["id"]})

    try:
        row = conn.execute("SELECT COUNT(*) FROM shared_resources WHERE request_status = 'pending'").fetchone()
        pending_count = row[0] if row else 0
        if pending_count > 0:
            alerts.append({"level": "info", "type": "pending_resources",
                            "message": f"有 {pending_count} 个共享资源请求待管理员审批", "node_id": None})
    except Exception:
        pass

    try:
        expiring = conn.execute(
            "SELECT volume_name,cleanup_after FROM user_workspace_volumes "
            "WHERE lifecycle='temporary' AND status='active' AND cleanup_after>%s AND cleanup_after<=%s "
            "ORDER BY cleanup_after LIMIT 5",
            (now, now + 7 * 86400),
        ).fetchall()
        if expiring:
            names = "、".join(row["volume_name"] for row in expiring[:3])
            alerts.append({"level": "warning", "type": "workspace_expiring",
                           "message": f"临时 workspace {names} 将在 7 天内清理，请先迁移需保留的数据",
                           "node_id": None})
    except Exception:
        pass

    online_nodes = [n for n in all_nodes if n["status"] == "online"]
    total_mem = sum(n.get("memory_total_gb", 0) for n in online_nodes)
    used_mem = sum(n.get("memory_used_gb", 0) for n in online_nodes)
    if total_mem > 0 and used_mem / total_mem > 0.90:
        pct = int(used_mem / total_mem * 100)
        alerts.append({"level": "warning", "type": "memory_pressure",
                        "message": f"集群内存使用率 {pct}%（{int(used_mem)} / {int(total_mem)} GB），建议释放资源",
                        "node_id": None})

    gpu_idle_cutoff = now - 86400
    idle = [c for c in all_containers
            if c["status"] == "stopped" and c.get("gpus")
            and (c.get("updated_at") or 0) < gpu_idle_cutoff]
    if idle:
        names = "、".join(c["name"] for c in idle[:3])
        suffix = f" 等共 {len(idle)} 个" if len(idle) > 3 else ""
        alerts.append({"level": "warning", "type": "gpu_idle",
                        "message": f"容器 {names}{suffix} 已停止超过 24 小时，仍占用 GPU，建议删除或释放",
                        "node_id": None})

    return alerts


def register_metrics_routes(app, deps: dict[str, Any]):
    from ..schemas import SummaryResponse
    db = deps["db"]
    mark_stale_nodes = deps["mark_stale_nodes"]
    get_node = deps["get_node"]
    gpu_with_container = deps["gpu_with_container"]
    list_containers = deps["list_containers"]

    @app.get("/api/summary", response_model=SummaryResponse)
    @app.get("/api/metrics/cluster", response_model=SummaryResponse)
    def summary():
        with db() as conn:
            mark_stale_nodes(conn)
            node_rows = conn.execute("SELECT * FROM nodes ORDER BY hostname").fetchall()
            all_nodes = [get_node(conn, row["id"]) for row in node_rows]
            gpu_rows = conn.execute(
                """
                SELECT g.*, n.hostname, n.driver_pool, n.status AS node_status, n.maintenance AS node_maintenance,
                       n.schedulable AS node_schedulable, n.node_type
                FROM gpus g JOIN nodes n ON n.id = g.node_id
                ORDER BY n.hostname, g.slot
                """
            ).fetchall()
            all_gpus = [gpu_with_container(conn, row) for row in gpu_rows]
            all_containers = list_containers(conn)
            cpu_total = sum(node["cpu_total"] for node in all_nodes)
            memory_total = sum(node["memory_total_gb"] for node in all_nodes)
            disk_total = sum(node["disk_total_gb"] for node in all_nodes)
            cpu_used = sum(
                int(node["cpu_total"] * max(0, min(100, node.get("cpu_usage_percent", 0))) / 100)
                for node in all_nodes
            )
            memory_used = sum(node["memory_used_gb"] for node in all_nodes)
            disk_used = sum(node["disk_used_gb"] for node in all_nodes)
            return {
                "nodes_online": sum(1 for node in all_nodes if node["status"] == "online"),
                "nodes_total": len(all_nodes),
                "gpus_free": sum(
                    1
                    for gpu in all_gpus
                    if gpu["node_status"] == "online"
                    and gpu["node_schedulable"]
                    and not gpu["node_maintenance"]
                    and gpu["node_type"] != "storage"
                    and not gpu.get("containers")
                ),
                "gpus_total": len(all_gpus),
                "containers_running": sum(1 for c in all_containers if c["status"] == "running"),
                "containers_total": len(all_containers),
                "cpu_used": cpu_used,
                "cpu_total": cpu_total,
                "memory_used_gb": memory_used,
                "memory_total_gb": memory_total,
                "disk_used_gb": disk_used,
                "disk_total_gb": disk_total,
                "alerts": _build_alerts_standalone(conn, all_nodes, all_containers),
            }

    @app.get("/api/metrics/node-hardware")
    def node_hardware():
        with db() as conn:
            mark_stale_nodes(conn)
            nodes = conn.execute(
                """
                SELECT id, hostname, display_order, status, cpu_model, cpu_total, cpu_cores, cpu_sockets,
                       cpu_temperature_c, memory_total_gb, disk_total_gb,
                       cpu_used, memory_used_gb, disk_used_gb, last_seen, uptime_seconds, load_avg,
                       cpu_usage_percent, swap_total_gb, swap_used_gb,
                       network_interface, network_rx_bytes_per_sec, network_tx_bytes_per_sec,
                       cuda_driver_api_version
                FROM nodes
                ORDER BY display_order, hostname
                """
            ).fetchall()
            result = []
            for node in nodes:
                row = dict(node)
                row["gpus"] = conn.execute(
                    """
                    SELECT id, slot, model, vram_gb, vram_used_mb
                    FROM gpus
                    WHERE node_id = %s
                    ORDER BY slot
                    """,
                    (node["id"],),
                ).fetchall()
                container_counts = conn.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'running') AS running,
                        COUNT(*) AS total
                    FROM containers
                    WHERE node_id = %s
                    """,
                    (node["id"],),
                ).fetchone()
                row["containers_running"] = container_counts["running"]
                row["containers_total"] = container_counts["total"]
                result.append(row)
            return result

    @app.get("/api/metrics/nodes/{node_id}")
    def node_metrics(node_id: int):
        with db() as conn:
            return get_node(conn, node_id)

    @app.get("/api/metrics/nodes/{node_id}/history")
    def node_metrics_history(node_id: int, hours: int = 6):
        """返回节点指标历史（CPU%/内存%/GPU%），最多 7 天。"""
        hours = max(1, min(hours, 24 * 7))
        since = int(time.time()) - hours * 3600
        with db() as conn:
            rows = conn.execute(
                """
                SELECT sampled_at, cpu_pct, memory_pct, disk_pct,
                       gpu_avg_pct, gpu_avg_vram_pct, temperature_c,
                       network_rx_bytes_per_sec, network_tx_bytes_per_sec, network_interface
                FROM node_metrics_snapshots
                WHERE node_id = %s AND sampled_at >= %s
                ORDER BY sampled_at ASC
                """,
                (node_id, since),
            ).fetchall()
            return [dict(r) for r in rows]

    @app.on_event("startup")
    async def start_metrics_sampler():
        asyncio.create_task(_metrics_sampling_loop())

    async def _metrics_sampling_loop():
        """每 5 分钟采集一次所有在线节点指标，写入 node_metrics_snapshots。
        超过 7 天的历史数据自动清理，防止表无限增长。"""
        # 错开启动，避免与其他 startup 任务同时冲击 DB
        await asyncio.sleep(30)
        while True:
            try:
                with db() as conn:
                    ts = int(time.time())
                    node_rows = conn.execute(
                        "SELECT * FROM nodes WHERE status = 'online'"
                    ).fetchall()
                    for node in node_rows:
                        nid = node["id"]
                        cpu_pct = float(node.get("cpu_usage_percent") or 0)
                        mem_total = float(node.get("memory_total_gb") or 1)
                        mem_used = float(node.get("memory_used_gb") or 0)
                        memory_pct = min(100, mem_used / mem_total * 100) if mem_total > 0 else 0
                        disk_total = float(node.get("disk_total_gb") or 1)
                        disk_used = float(node.get("disk_used_gb") or 0)
                        disk_pct = min(100, disk_used / disk_total * 100) if disk_total > 0 else 0
                        gpus = conn.execute(
                            "SELECT utilization, vram_gb, vram_used_mb FROM gpus WHERE node_id = %s",
                            (nid,),
                        ).fetchall()
                        if gpus:
                            gpu_avg_pct = sum(g["utilization"] for g in gpus) / len(gpus)
                            gpu_avg_vram_pct = sum(
                                (g["vram_used_mb"] / 1024 / g["vram_gb"] * 100) if g["vram_gb"] > 0 else 0
                                for g in gpus
                            ) / len(gpus)
                        else:
                            gpu_avg_pct = gpu_avg_vram_pct = 0
                        temperature_c = int(node.get("cpu_temperature_c") or 0)
                        network_rx_bytes_per_sec = max(0, float(node.get("network_rx_bytes_per_sec") or 0))
                        network_tx_bytes_per_sec = max(0, float(node.get("network_tx_bytes_per_sec") or 0))
                        network_interface = str(node.get("network_interface") or "")[:64]
                        conn.execute(
                            """
                            INSERT INTO node_metrics_snapshots
                                (node_id, sampled_at, cpu_pct, memory_pct, disk_pct,
                                 gpu_avg_pct, gpu_avg_vram_pct, temperature_c,
                                 network_rx_bytes_per_sec, network_tx_bytes_per_sec, network_interface)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (nid, ts, cpu_pct, memory_pct, disk_pct,
                             gpu_avg_pct, gpu_avg_vram_pct, temperature_c,
                             network_rx_bytes_per_sec, network_tx_bytes_per_sec, network_interface),
                        )
                    # 清理 7 天以上的旧数据
                    cutoff = ts - 7 * 86400
                    conn.execute(
                        "DELETE FROM node_metrics_snapshots WHERE sampled_at < %s", (cutoff,)
                    )
            except Exception as exc:
                import sys
                print(f"[WARN] _metrics_sampling_loop: {exc!r}", file=sys.stderr, flush=True)
            await asyncio.sleep(300)  # 5 分钟
