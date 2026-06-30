from typing import Any


def register_metrics_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    mark_stale_nodes = deps["mark_stale_nodes"]
    get_node = deps["get_node"]
    gpu_with_container = deps["gpu_with_container"]
    list_containers = deps["list_containers"]

    @app.get("/api/summary")
    @app.get("/api/metrics/cluster")
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
                "alerts": [],
            }

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
