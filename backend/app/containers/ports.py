import secrets
from typing import Any

from fastapi import HTTPException

from ..agent.tasks import enqueue_node_task
from ..config import NODE_PORT_RANGE_END, NODE_PORT_RANGE_START, PORT_RANGE_END, PORT_RANGE_START
from ..core import now_ts
from ..schemas import ContainerPortInput

def managed_ssh_keys(user_key: str) -> str:
    keys = []
    for line in user_key.splitlines():
        line = line.strip()
        if line and line not in keys:
            keys.append(line)
    return "\n".join(keys)

def list_container_ports(conn, container_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM container_ports
        WHERE container_id = %s
        ORDER BY host_port, id
        """,
        (container_id,),
    ).fetchall()
    return [public_port_payload(row) for row in rows]

def normalize_port_payload(payload: ContainerPortInput) -> ContainerPortInput:
    protocol = payload.protocol.lower().strip()
    if protocol not in {"tcp", "udp"}:
        raise HTTPException(status_code=400, detail="端口协议只支持 tcp 或 udp")
    if payload.container_port < 1 or payload.container_port > 65535:
        raise HTTPException(status_code=400, detail="容器内端口必须在 1-65535 之间")
    payload.protocol = protocol
    payload.name = payload.name.strip()
    return payload

def allocate_port_from_pool(conn, column: str, start: int, end: int, label: str) -> int:
    if column not in {"host_port", "node_port"}:
        raise HTTPException(status_code=500, detail="端口列配置不合法")
    if start < 1 or end > 65535 or start > end:
        raise HTTPException(status_code=500, detail=f"{label}端口范围配置不合法")
    size = end - start + 1
    for _ in range(min(size, 2000)):
        candidate = start + secrets.randbelow(size)
        exists = conn.execute(f"SELECT 1 FROM container_ports WHERE {column} = %s", (candidate,)).fetchone()
        if not exists:
            return candidate
    row = conn.execute(
        f"""
        SELECT used.{column} + 1 AS port
        FROM container_ports used
        WHERE used.{column} BETWEEN %s AND %s
          AND NOT EXISTS (
            SELECT 1 FROM container_ports next_used
            WHERE next_used.{column} = used.{column} + 1
          )
        ORDER BY used.{column}
        LIMIT 1
        """,
        (start, end),
    ).fetchone()
    if row and row["port"] <= end:
        return row["port"]
    first_used = conn.execute(f"SELECT 1 FROM container_ports WHERE {column} = %s", (start,)).fetchone()
    if not first_used:
        return start
    raise HTTPException(status_code=409, detail=f"{label}端口池已用尽")

def allocate_host_port(conn) -> int:
    return allocate_port_from_pool(conn, "host_port", PORT_RANGE_START, PORT_RANGE_END, "外部")

def allocate_node_port(conn) -> int:
    return allocate_port_from_pool(conn, "node_port", NODE_PORT_RANGE_START, NODE_PORT_RANGE_END, "节点")

def backfill_node_ports(conn):
    rows = conn.execute("SELECT id FROM container_ports WHERE node_port = 0 ORDER BY id").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE container_ports SET node_port = %s, updated_at = %s WHERE id = %s",
            (allocate_node_port(conn), now_ts(), row["id"]),
        )

def add_container_port(conn, container_id: int, payload: ContainerPortInput) -> dict[str, Any]:
    payload = normalize_port_payload(payload)
    if not conn.execute("SELECT 1 FROM containers WHERE id = %s", (container_id,)).fetchone():
        raise HTTPException(status_code=404, detail="容器不存在")
    duplicated = conn.execute(
        """
        SELECT 1 FROM container_ports
        WHERE container_id = %s AND protocol = %s AND container_port = %s
        """,
        (container_id, payload.protocol, payload.container_port),
    ).fetchone()
    if duplicated:
        raise HTTPException(status_code=409, detail="该容器内端口映射已存在")
    ts = now_ts()
    return conn.execute(
        """
        INSERT INTO container_ports (
            container_id, name, protocol, container_port, host_port, node_port, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            container_id,
            payload.name,
            payload.protocol,
            payload.container_port,
            allocate_host_port(conn),
            allocate_node_port(conn),
            ts,
            ts,
        ),
    ).fetchone()

def public_port_payload(port: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": port["id"],
        "name": port["name"],
        "protocol": port["protocol"],
        "container_port": port["container_port"],
        "host_port": port["host_port"],
        "node_port": port["node_port"],
        "public_port": port["host_port"],
        "node_listen_port": port["node_port"],
    }

def incus_ports_payload(container: dict[str, Any], ports: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "container_id": container["id"],
        "name": container["name"],
        "ssh_username": container["ssh_username"],
        "ssh_key": managed_ssh_keys(container["ssh_key"]),
        "mounts": container["mounts"] or [],
        "managed_mounts": container.get("managed_mounts") or [],
        "ports": [public_port_payload(port) for port in ports],
    }

def enqueue_running_port_syncs(conn):
    containers = conn.execute("SELECT * FROM containers WHERE status = 'running' ORDER BY id").fetchall()
    for container in containers:
        ports = list_container_ports(conn, container["id"])
        if not ports:
            continue
        pending = conn.execute(
            """
            SELECT 1 FROM node_tasks
            WHERE container_id = %s
              AND task_type = 'incus_sync_ports'
              AND status IN ('pending', 'running')
            LIMIT 1
            """,
            (container["id"],),
        ).fetchone()
        if pending:
            continue
        enqueue_node_task(
            conn,
            container["node_id"],
            container["id"],
            "incus_sync_ports",
            incus_ports_payload(container, ports),
        )
