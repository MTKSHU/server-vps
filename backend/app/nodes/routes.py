import re
import os
import secrets
import socket
from typing import Any

import asyncssh
from fastapi import HTTPException, Request
from psycopg.types.json import Jsonb

from ..schemas import JoinTokenCreate, NodeConfigInput
from ..auth import require_admin
from ..auth import is_admin_user
from .services import allowed_node_ids_for_user

_SSH_KEY_PATH = os.path.join(
    os.environ.get("AGENT_RELEASE_DIR", "/var/lib/cluster-agent-releases"),
    ".cluster_node_key",
)


def _get_or_create_ssh_key():
    if not os.path.exists(_SSH_KEY_PATH):
        key = asyncssh.generate_private_key("ssh-ed25519")
        os.makedirs(os.path.dirname(_SSH_KEY_PATH), exist_ok=True)
        key.write_private_key(_SSH_KEY_PATH)
    return asyncssh.read_private_key(_SSH_KEY_PATH)


def get_node_ssh_pubkey_str() -> str:
    key = _get_or_create_ssh_key()
    pubkey = key.export_public_key()
    if isinstance(pubkey, bytes):
        return pubkey.decode().strip()
    return str(pubkey).strip()


def generate_ephemeral_sync_keypair() -> tuple[str, str]:
    # 使用 RSA + PKCS#1 PEM 格式私钥，兼容性更好；部分旧版 OpenSSH/libcrypto
    # 无法直接加载 asyncssh 默认 OpenSSH 格式的 ed25519 临时私钥。
    key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    privkey = key.export_private_key(format_name="pkcs1-pem")
    pubkey = key.export_public_key()
    if isinstance(privkey, bytes):
        privkey = privkey.decode()
    if isinstance(pubkey, bytes):
        pubkey = pubkey.decode().strip()
    return privkey.strip(), pubkey


def public_join_token(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "token_preview": row["token_preview"],
        "expected_hostname": row["expected_hostname"],
        "node_group": row["node_group"],
        "note": row["note"],
        "status": row["status"],
        "node_id": row["node_id"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "used_at": row["used_at"],
        "server_url": row.get("server_url", ""),
    }


def join_command(server_url: str, token: str, expected_hostname: str = "") -> str:
    hostname_arg = f" --hostname {expected_hostname}" if expected_hostname else ""
    return (
        "cluster-node-agent"
        f" --server {server_url}"
        f" --token {token}"
        f"{hostname_arg}"
        " --data-path /data"
    )


def normalize_node_config(payload: NodeConfigInput) -> NodeConfigInput:
    for key in (
        "max_containers",
        "max_running_containers",
        "max_gpu_shared_containers",
        "max_cpu_per_container",
        "max_memory_gb_per_container",
        "max_disk_gb_per_container",
        "reserved_memory_gb",
        "reserved_disk_gb",
        "max_ports_per_container",
    ):
        if getattr(payload, key) < 0:
            raise HTTPException(status_code=400, detail=f"{key} 不能为负数")
    if payload.scheduler_weight < -1000 or payload.scheduler_weight > 1000:
        raise HTTPException(status_code=400, detail="scheduler_weight 必须在 -1000 到 1000 之间")
    normalized_labels = []
    for label in payload.labels:
        value = label.strip().lower()
        if not value:
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,40}", value):
            raise HTTPException(status_code=400, detail=f"节点标签不合法：{label}")
        if value not in normalized_labels:
            normalized_labels.append(value)
    if len(normalized_labels) > 20:
        raise HTTPException(status_code=400, detail="节点标签最多 20 个")
    payload.labels = normalized_labels
    payload.wol_mac = payload.wol_mac.strip().lower()
    if payload.wol_mac and not re.fullmatch(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", payload.wol_mac):
        raise HTTPException(status_code=400, detail="WOL MAC 地址格式不合法")
    payload.wol_broadcast = payload.wol_broadcast.strip() or "255.255.255.255"
    try:
        socket.inet_aton(payload.wol_broadcast)
    except OSError:
        raise HTTPException(status_code=400, detail="WOL 广播地址不合法")
    payload.ssh_user = payload.ssh_user.strip() or "root"
    if not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", payload.ssh_user):
        raise HTTPException(status_code=400, detail="SSH 用户名格式不合法")
    if not (1 <= payload.ssh_port <= 65535):
        raise HTTPException(status_code=400, detail="SSH 端口必须在 1-65535 之间")
    payload.sync_ip = payload.sync_ip.strip()
    if payload.sync_ssh_port < 0 or payload.sync_ssh_port > 65535:
        raise HTTPException(status_code=400, detail="同步 SSH 端口必须在 0-65535 之间")
    payload.resource_cache_base = payload.resource_cache_base.strip()
    if payload.resource_cache_base:
        rcb = payload.resource_cache_base
        if not rcb.startswith("/"):
            raise HTTPException(status_code=400, detail="资源缓存目录必须是绝对路径")
        if "\x00" in rcb or "/../" in rcb or rcb.endswith("/.."):
            raise HTTPException(status_code=400, detail="资源缓存目录不合法")
        if len(rcb) > 240:
            raise HTTPException(status_code=400, detail="资源缓存目录路径过长")
    return payload


def send_wake_on_lan(mac: str, broadcast: str):
    normalized = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(normalized) != 12:
        raise HTTPException(status_code=400, detail="请先配置节点 WOL MAC 地址")
    packet = bytes.fromhex("ff" * 6 + normalized * 16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, 9))


def register_node_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]
    audit = deps["audit"]
    hash_token = deps["hash_token"]
    mark_stale_nodes = deps["mark_stale_nodes"]
    get_node = deps["get_node"]
    gpu_with_container = deps["gpu_with_container"]
    enqueue_node_task = deps["enqueue_node_task"]
    public_task = deps["public_task"]
    current_user = deps["current_user"]

    from ..schemas import NodeOut

    @app.get("/api/nodes", response_model=list[NodeOut])
    def nodes():
        with db() as conn:
            user = current_user(conn)
            mark_stale_nodes(conn)
            rows = conn.execute("SELECT * FROM nodes ORDER BY hostname").fetchall()
            if not is_admin_user(user) or user.get("group_name") != "platform_admin":
                allowed_ids = allowed_node_ids_for_user(conn, user)
                if allowed_ids is not None:
                    rows = [row for row in rows if row["id"] in allowed_ids]
            return [get_node(conn, row["id"]) for row in rows]

    @app.get("/api/nodes/ssh-pubkey")
    def node_ssh_pubkey():
        require_admin()
        return {"pubkey": get_node_ssh_pubkey_str()}

    @app.post("/api/nodes/{node_id}/install-ssh-pubkey")
    def install_node_ssh_pubkey(node_id: int):
        require_admin()
        with db() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="节点不存在")
            pubkey = get_node_ssh_pubkey_str()
            task = enqueue_node_task(conn, node_id, None, "ssh_pubkey_install", {"pubkey": pubkey})
            return public_task(task)

    @app.put("/api/nodes/{node_id}/config")
    def update_node_config(node_id: int, payload: NodeConfigInput):
        require_admin()
        payload = normalize_node_config(payload)
        with db() as conn:
            row = conn.execute(
                """
                UPDATE nodes
                SET node_type = %s,
                    schedulable = %s,
                    maintenance = %s,
                    max_containers = %s,
                    max_running_containers = %s,
                    max_gpu_shared_containers = %s,
                    allow_gpu_sharing = %s,
                    max_cpu_per_container = %s,
                    max_memory_gb_per_container = %s,
                    max_disk_gb_per_container = %s,
                    reserved_memory_gb = %s,
                    reserved_disk_gb = %s,
                    allow_port_mapping = %s,
                    max_ports_per_container = %s,
                    scheduler_weight = %s,
                    labels = %s,
                    wol_mac = %s,
                    wol_broadcast = %s,
                    ssh_user = %s,
                    ssh_port = %s,
                    sync_ip = %s,
                    sync_ssh_port = %s,
                    resource_cache_base = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    payload.node_type,
                    payload.schedulable,
                    payload.maintenance,
                    payload.max_containers,
                    payload.max_running_containers,
                    payload.max_gpu_shared_containers,
                    payload.allow_gpu_sharing,
                    payload.max_cpu_per_container,
                    payload.max_memory_gb_per_container,
                    payload.max_disk_gb_per_container,
                    payload.reserved_memory_gb,
                    payload.reserved_disk_gb,
                    payload.allow_port_mapping,
                    payload.max_ports_per_container,
                    payload.scheduler_weight,
                    Jsonb(payload.labels),
                    payload.wol_mac,
                    payload.wol_broadcast,
                    payload.ssh_user,
                    payload.ssh_port,
                    payload.sync_ip,
                    payload.sync_ssh_port,
                    payload.resource_cache_base,
                    node_id,
                ),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="节点不存在")
            audit(conn, "admin", "update-config", f"node:{node_id}", payload.model_dump())
            return get_node(conn, node_id)

    @app.delete("/api/nodes/{node_id}")
    def delete_node(node_id: int):
        require_admin()
        with db() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="节点不存在")
            container_count = conn.execute(
                "SELECT COUNT(*) AS count FROM containers WHERE node_id = %s",
                (node_id,),
            ).fetchone()["count"]
            if container_count:
                raise HTTPException(status_code=409, detail="该节点仍有关联容器，请先删除或迁移容器")
            conn.execute("DELETE FROM nodes WHERE id = %s", (node_id,))
            audit(conn, "admin", "delete", f"node:{node_id}", {"hostname": node["hostname"]})
            return {"ok": True, "node_id": node_id}

    @app.post("/api/nodes/{node_id}/shutdown", status_code=202)
    def shutdown_node(node_id: int):
        require_admin()
        with db() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="节点不存在")
            task = enqueue_node_task(conn, node_id, None, "node_shutdown", {"hostname": node["hostname"]})
            audit(conn, "admin", "shutdown", f"node:{node_id}", {"hostname": node["hostname"]})
            return public_task(task)

    @app.post("/api/nodes/{node_id}/reboot", status_code=202)
    def reboot_node(node_id: int):
        require_admin()
        with db() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="节点不存在")
            task = enqueue_node_task(conn, node_id, None, "node_reboot", {"hostname": node["hostname"]})
            audit(conn, "admin", "reboot", f"node:{node_id}", {"hostname": node["hostname"]})
            return public_task(task)

    @app.post("/api/nodes/{node_id}/trigger-agent-update", status_code=202)
    def trigger_agent_update(node_id: int):
        require_admin()
        with db() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="节点不存在")
            conn.execute(
                """
                UPDATE nodes
                SET agent_auto_update = TRUE,
                    target_agent_version = '',
                    agent_update_status = 'idle',
                    agent_update_error = ''
                WHERE id = %s
                """,
                (node_id,),
            )
            task = enqueue_node_task(conn, node_id, None, "trigger_agent_update", {"hostname": node["hostname"]})
            audit(
                conn,
                "admin",
                "trigger-agent-update",
                f"node:{node_id}",
                {"hostname": node["hostname"], "target_version": "", "auto_update": True},
            )
            return public_task(task)

    @app.post("/api/nodes/{node_id}/wake", status_code=202)
    def wake_node(node_id: int):
        require_admin()
        with db() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,)).fetchone()
            if not node:
                raise HTTPException(status_code=404, detail="节点不存在")
            if not node["wol_mac"]:
                raise HTTPException(status_code=400, detail="请先在节点配置里填写 WOL MAC 地址")
            send_wake_on_lan(node["wol_mac"], node["wol_broadcast"] or "255.255.255.255")
            audit(conn, "admin", "wake", f"node:{node_id}", {"hostname": node["hostname"], "wol_mac": node["wol_mac"]})
            return {"ok": True, "node_id": node_id}

    @app.get("/api/gpus")
    def gpus():
        with db() as conn:
            mark_stale_nodes(conn)
            rows = conn.execute(
                """
                SELECT g.*, n.hostname, n.driver_pool, n.status AS node_status
                FROM gpus g JOIN nodes n ON n.id = g.node_id
                ORDER BY n.hostname, g.slot
                """
            ).fetchall()
            return [gpu_with_container(conn, row) for row in rows]

    @app.get("/api/node-join-tokens")
    def node_join_tokens():
        require_admin()
        with db() as conn:
            conn.execute(
                "UPDATE node_join_tokens SET status = 'expired' WHERE status = 'pending' AND expires_at < %s",
                (now_ts(),),
            )
            rows = conn.execute("SELECT * FROM node_join_tokens ORDER BY created_at DESC, id DESC").fetchall()
            return [public_join_token(row) for row in rows]

    @app.post("/api/node-join-tokens", status_code=201)
    def create_node_join_token(payload: JoinTokenCreate, request: Request):
        require_admin()
        if payload.expected_hostname and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{1,62}", payload.expected_hostname):
            raise HTTPException(status_code=400, detail="预期 hostname 不合法")
        if payload.expires_in_hours < 1 or payload.expires_in_hours > 720:
            raise HTTPException(status_code=400, detail="有效期必须在 1-720 小时之间")
        token = secrets.token_urlsafe(32)
        server_url = payload.server_url.strip() or str(request.base_url).rstrip("/")
        ts = now_ts()
        with db() as conn:
            row = conn.execute(
                """
                INSERT INTO node_join_tokens (
                    token_hash, token_preview, expected_hostname, node_group, note,
                    server_url, status, created_by, created_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', 'admin', %s, %s)
                RETURNING *
                """,
                (
                    hash_token(token),
                    token[-8:],
                    payload.expected_hostname.strip(),
                    "unassigned",
                    payload.note.strip(),
                    server_url,
                    ts,
                    ts + payload.expires_in_hours * 3600,
                ),
            ).fetchone()
            audit(conn, "admin", "create", f"node-join-token:{row['id']}", {"expected_hostname": payload.expected_hostname})
            public = public_join_token(row)
            public["token"] = token
            public["server_url"] = server_url
            public["command"] = join_command(server_url, token, row["expected_hostname"])
            public["env_file"] = "\n".join(
                [
                    f"CLUSTER_SERVER_URL={server_url}",
                    f"CLUSTER_NODE_TOKEN={token}",
                    "CLUSTER_DATA_PATH=/data",
                ]
            )
            return public

    @app.delete("/api/node-join-tokens/{token_id}")
    def delete_node_join_token(token_id: int):
        require_admin()
        with db() as conn:
            row = conn.execute("SELECT * FROM node_join_tokens WHERE id = %s", (token_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="token 不存在")
            conn.execute("DELETE FROM node_join_tokens WHERE id = %s", (token_id,))
            audit(conn, "admin", "delete", f"node-join-token:{token_id}", {"expected_hostname": row["expected_hostname"]})
            return {"ok": True}
