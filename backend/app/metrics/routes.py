from typing import Any
import time


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
                "alerts": _build_alerts(conn, all_nodes, all_containers),
            }

    def _build_alerts(conn, all_nodes: list, all_containers: list) -> list[dict]:
        alerts = []
        now = int(time.time())
        stale_cutoff = now - 300  # 5 分钟无心跳视为真正离线

        # 1. 节点离线（stale/offline 状态）
        for node in all_nodes:
            if node["status"] != "online":
                last_seen = node.get("last_seen") or 0
                if last_seen < stale_cutoff:
                    alerts.append({
                        "level": "error",
                        "type": "node_offline",
                        "message": f"节点 {node['hostname']} 已离线",
                        "node_id": node["id"],
                    })

        # 2. 磁盘使用率过高（> 85%）
        for node in all_nodes:
            if node["status"] == "online" and node.get("disk_total_gb", 0) > 0:
                used_pct = node.get("disk_used_gb", 0) / node["disk_total_gb"]
                if used_pct > 0.85:
                    alerts.append({
                        "level": "warning",
                        "type": "disk_full",
                        "message": f"节点 {node['hostname']} 磁盘使用率 {int(used_pct * 100)}%，建议清理",
                        "node_id": node["id"],
                    })

        # 3. 待审批的共享资源请求
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM shared_resources WHERE request_status = 'pending'"
            ).fetchone()
            pending_count = row[0] if row else 0
            if pending_count > 0:
                alerts.append({
                    "level": "info",
                    "type": "pending_resources",
                    "message": f"有 {pending_count} 个共享资源请求待管理员审批",
                    "node_id": None,
                })
        except Exception:
            pass  # 表不存在时静默跳过

        # 4. 集群内存压力（在线节点总使用率 > 90%）
        online_nodes = [n for n in all_nodes if n["status"] == "online"]
        total_mem = sum(n.get("memory_total_gb", 0) for n in online_nodes)
        used_mem = sum(n.get("memory_used_gb", 0) for n in online_nodes)
        if total_mem > 0 and used_mem / total_mem > 0.90:
            pct = int(used_mem / total_mem * 100)
            alerts.append({
                "level": "warning",
                "type": "memory_pressure",
                "message": f"集群内存使用率 {pct}%（{int(used_mem)} / {int(total_mem)} GB），建议释放资源",
                "node_id": None,
            })

        # 5. GPU 长期空转（有 GPU 分配但容器已停止超过 24 小时）
        gpu_idle_cutoff = now - 86400
        idle = [
            c for c in all_containers
            if c["status"] == "stopped"
            and c.get("gpus")
            and (c.get("updated_at") or 0) < gpu_idle_cutoff
        ]
        if idle:
            names = "、".join(c["name"] for c in idle[:3])
            suffix = f" 等共 {len(idle)} 个" if len(idle) > 3 else ""
            alerts.append({
                "level": "warning",
                "type": "gpu_idle",
                "message": f"容器 {names}{suffix} 已停止超过 24 小时，仍占用 GPU，建议删除或释放",
                "node_id": None,
            })

        return alerts

    @app.get("/api/metrics/node-hardware")
    def node_hardware():
        with db() as conn:
            mark_stale_nodes(conn)
            nodes = conn.execute(
                """
                SELECT id, hostname, status, cpu_model, cpu_total, cpu_cores, cpu_sockets,
                       cpu_temperature_c, memory_total_gb, disk_total_gb,
                       cpu_used, memory_used_gb, disk_used_gb, last_seen, uptime_seconds, load_avg,
                       cpu_usage_percent, swap_total_gb, swap_used_gb,
                       cuda_driver_api_version
                FROM nodes
                ORDER BY hostname
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
