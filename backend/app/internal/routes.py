from typing import Any

from fastapi import HTTPException, Request

from ..config import PORT_ROUTER_TOKEN


def register_internal_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]

    @app.get("/api/internal/port-routes")
    def internal_port_routes(request: Request):
        token = request.headers.get("x-port-router-token", "")
        if PORT_ROUTER_TOKEN and token != PORT_ROUTER_TOKEN:
            raise HTTPException(status_code=403, detail="port router token 不匹配")
        with db() as conn:
            rows = conn.execute(
                """
                SELECT
                    cp.id,
                    cp.container_id,
                    cp.name,
                    cp.protocol,
                    cp.container_port,
                    cp.host_port,
                    cp.node_port,
                    c.name AS container_name,
                    c.status AS container_status,
                    n.id AS node_id,
                    n.hostname AS node_hostname,
                    n.ip AS node_ip,
                    n.status AS node_status
                FROM container_ports cp
                JOIN containers c ON c.id = cp.container_id
                JOIN nodes n ON n.id = c.node_id
                WHERE c.status = 'running'
                  AND n.ip != ''
                  AND n.ip != 'unknown'
                  AND cp.node_port > 0
                ORDER BY cp.host_port, cp.id
                """
            ).fetchall()
        return {"routes": rows, "updated_at": now_ts()}
