from typing import Any

from fastapi import HTTPException, Request

from ..config import PORT_ROUTER_TOKEN

# 允许通过路径代理（/c/<container-name>/）访问的端口名称集合
_WEB_PORT_NAMES = {"code-server", "jupyterlab", "web"}


def _check_router_token(request: Request) -> None:
    token = request.headers.get("x-port-router-token", "")
    if PORT_ROUTER_TOKEN and token != PORT_ROUTER_TOKEN:
        raise HTTPException(status_code=403, detail="port router token 不匹配")


def register_internal_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]

    @app.get("/api/internal/port-routes")
    def internal_port_routes(request: Request):
        _check_router_token(request)
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

    @app.get("/api/internal/path-routes")
    def internal_path_routes(request: Request):
        """返回可通过路径代理（/c/<container>/<port-name>/）访问的 Web 端口路由表。
        每个 (container_name, port_name) 对是一条独立路由，同一容器可有多条。
        """
        _check_router_token(request)
        with db() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.name        AS container_name,
                    n.ip          AS node_ip,
                    cp.node_port  AS node_port,
                    cp.name       AS port_name,
                    cp.container_port
                FROM container_ports cp
                JOIN containers c ON c.id = cp.container_id
                JOIN nodes n      ON n.id = c.node_id
                WHERE c.status = 'running'
                  AND n.ip NOT IN ('', 'unknown')
                  AND cp.node_port > 0
                  AND cp.protocol = 'tcp'
                  AND cp.name = ANY(%s)
                ORDER BY c.name, cp.id
                """,
                [list(_WEB_PORT_NAMES)],
            ).fetchall()
        return {"routes": rows, "updated_at": now_ts()}
