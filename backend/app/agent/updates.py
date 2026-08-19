import asyncio
import hashlib
import os
import re
import socket
from pathlib import Path
from typing import Any

import docker as docker_sdk
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from ..auth import require_admin
from ..config import AGENT_RELEASE_DIR, AGENT_SOURCE_HOST_PATH


VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
ARCHITECTURES = {"amd64", "arm64"}
CHANNELS = {"stable", "canary"}
UPDATE_STATUSES = {"checking", "downloading", "installing", "updated", "failed", "rolled_back"}


def _clean(value: str, allowed: set[str], label: str) -> str:
    value = value.strip().lower()
    if value not in allowed:
        raise HTTPException(status_code=400, detail=f"{label} 不合法")
    return value


def _release_path(file_name: str) -> Path:
    root = Path(AGENT_RELEASE_DIR).resolve()
    path = (root / file_name).resolve()
    if path.parent != root:
        raise HTTPException(status_code=400, detail="发布文件路径不合法")
    return path


def _agent_credentials(request: Request) -> tuple[str, str]:
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else request.headers.get("x-agent-token", "")
    return token, request.headers.get("x-agent-hostname", "")


def _public_release(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("version", "architecture", "channel", "sha256", "size_bytes", "enabled", "created_at", "changelog")}


def register_agent_update_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]
    audit = deps["audit"]
    verify_agent_node = deps["verify_agent_node"]

    @app.get("/api/agent-releases")
    def list_releases():
        require_admin()
        with db() as conn:
            rows = conn.execute("SELECT * FROM agent_releases ORDER BY created_at DESC, version DESC").fetchall()
            return [_public_release(row) for row in rows]

    @app.post("/api/agent-releases/{version}/build", status_code=201)
    async def build_release(version: str, request: Request, architecture: str = "amd64", channel: str = "stable"):
        """通过宿主机 Docker daemon 启动 Go 编译容器，将编译产物写入 agent-releases 卷。"""
        require_admin()
        if not VERSION_RE.fullmatch(version):
            raise HTTPException(status_code=400, detail="版本号不合法")
        architecture = _clean(architecture, ARCHITECTURES, "架构")
        channel = _clean(channel, CHANNELS, "更新通道")
        try:
            body = await request.json()
        except Exception:
            body = {}
        changelog = str(body.get("changelog", ""))[:4000].strip()
        if not AGENT_SOURCE_HOST_PATH:
            raise HTTPException(
                status_code=500,
                detail="未配置 AGENT_SOURCE_HOST_PATH 环境变量"
            )
        file_name = f"cluster-node-agent-{version}-{architecture}"
        goarch = architecture
        output_path = f"{AGENT_RELEASE_DIR}/{file_name}"
        updater_file_name = f"cluster-agent-updater-{architecture}"
        updater_output_path = f"{AGENT_RELEASE_DIR}/{updater_file_name}"
        build_cmd = (
            f"set -e && cd /src && "
            f"export CGO_ENABLED=0 GOOS=linux GOARCH={goarch} "
            f"GOPROXY=https://goproxy.cn,direct && "
            f"go build -ldflags='-s -w -X main.agentVersion={version}' "
            f"-o {output_path} ./cmd/node-agent/ && "
            f"chmod 755 {output_path} && "
            f"go build -ldflags='-s -w' "
            f"-o {updater_output_path} ./cmd/agent-updater/ && "
            f"chmod 755 {updater_output_path}"
        )

        def _run_build() -> None:
            try:
                client = docker_sdk.from_env()
            except docker_sdk.errors.DockerException as exc:
                raise HTTPException(status_code=500, detail=f"无法连接 Docker socket，请确认 /var/run/docker.sock 已挂载并授权：{exc}")
            # 通过 volumes_from 继承后端容器的卷挂载，编译容器可直接写入 AGENT_RELEASE_DIR
            backend_id = socket.gethostname()
            proxy_environment = {
                key: value
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
                if (value := os.environ.get(key, "").strip())
            }
            try:
                client.containers.run(
                    "golang:1.23",
                    ["sh", "-c", build_cmd],
                    remove=True,
                    # The configured proxy listens on the host loopback, so the
                    # build container must share the host network to reach it.
                    network_mode="host",
                    environment=proxy_environment,
                    volumes={
                        AGENT_SOURCE_HOST_PATH: {"bind": "/src", "mode": "ro"},
                    },
                    volumes_from=[backend_id],
                    stdout=True,
                    stderr=True,
                )
            except docker_sdk.errors.ContainerError as exc:
                stderr = exc.stderr.decode(errors="replace")[-3000:] if exc.stderr else str(exc)
                raise HTTPException(status_code=500, detail=f"编译失败：\n{stderr}")
            except docker_sdk.errors.ImageNotFound:
                raise HTTPException(status_code=500, detail="编译镜像 golang:1.23 不存在，请确认宿主机可拉取该镜像")
            except docker_sdk.errors.APIError as exc:
                raise HTTPException(status_code=500, detail=f"Docker API 错误：{exc}")
            finally:
                client.close()

        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, _run_build), timeout=600)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="编译超时（600s），请检查网络和 Go 模块缓存")

        path = _release_path(file_name)
        if not path.is_file():
            raise HTTPException(status_code=500, detail="编译完成但输出文件未找到，请检查卷挂载配置")
        size = path.stat().st_size
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        with db() as conn:
            row = conn.execute(
                """
                INSERT INTO agent_releases (version, architecture, channel, sha256, size_bytes, file_name, changelog, enabled, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                ON CONFLICT (version, architecture) DO UPDATE SET
                    channel = EXCLUDED.channel, sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes,
                    file_name = EXCLUDED.file_name, changelog = EXCLUDED.changelog, enabled = TRUE, created_at = EXCLUDED.created_at
                RETURNING *
                """,
                (version, architecture, channel, sha256, size, file_name, changelog, now_ts()),
            ).fetchone()
            audit(conn, "admin", "build", f"agent-release:{version}:{architecture}", _public_release(row))
            return _public_release(row)

    @app.delete("/api/agent-releases/{version}")
    def delete_release(version: str, architecture: str = "amd64"):
        require_admin()
        if not VERSION_RE.fullmatch(version):
            raise HTTPException(status_code=400, detail="版本号不合法")
        architecture = _clean(architecture, ARCHITECTURES, "架构")
        with db() as conn:
            row = conn.execute(
                "DELETE FROM agent_releases WHERE version = %s AND architecture = %s RETURNING *",
                (version, architecture),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="版本不存在")
            audit(conn, "admin", "delete", f"agent-release:{version}:{architecture}", {})
        try:
            path = _release_path(row["file_name"])
            if path.is_file():
                path.unlink()
        except Exception:
            pass
        return {"ok": True}

    @app.get("/api/agent-releases/latest/download")
    def download_latest_agent_release(architecture: str = "amd64"):
        # Bootstrap endpoint: a node does not have a registered agent identity
        # yet, and a join token is deliberately not a Web UI bearer session.
        # Release contents are executable artifacts rather than credentials;
        # integrity/authenticity is provided by TLS during initial install.
        architecture = _clean(architecture, ARCHITECTURES, "架构")
        with db() as conn:
            release = conn.execute(
                """
                SELECT * FROM agent_releases
                WHERE channel = 'stable' AND architecture = %s AND enabled = TRUE
                ORDER BY created_at DESC LIMIT 1
                """,
                (architecture,),
            ).fetchone()
        if not release:
            raise HTTPException(status_code=404, detail="尚无可用的 stable 发布版本")
        path = _release_path(release["file_name"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="二进制文件缺失")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename="cluster-node-agent",
        )

    @app.get("/api/agent-releases/latest/download-updater")
    def download_latest_agent_updater(architecture: str = "amd64"):
        # Public for the same bootstrap reason as latest/download.  Updater
        # manifest and versioned update downloads remain node-token protected.
        architecture = _clean(architecture, ARCHITECTURES, "架构")
        file_name = f"cluster-agent-updater-{architecture}"
        path = _release_path(file_name)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="updater 二进制缺失，请先在「Agent 发布」中编译一个版本")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename="cluster-agent-updater",
        )

    @app.get("/api/agent-releases/{version}/download")
    def admin_download_release(version: str, architecture: str = "amd64"):
        require_admin()
        if not VERSION_RE.fullmatch(version):
            raise HTTPException(status_code=400, detail="版本号不合法")
        architecture = _clean(architecture, ARCHITECTURES, "架构")
        with db() as conn:
            release = conn.execute(
                "SELECT * FROM agent_releases WHERE version = %s AND architecture = %s",
                (version, architecture),
            ).fetchone()
        if not release:
            raise HTTPException(status_code=404, detail="版本不存在")
        path = _release_path(release["file_name"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="二进制文件缺失")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=f"cluster-node-agent-{version}-{architecture}",
        )

    @app.put("/api/nodes/{node_id}/agent-update")
    async def configure_node_update(node_id: int, request: Request):
        require_admin()
        payload = await request.json()
        channel = _clean(str(payload.get("channel", "stable")), CHANNELS, "更新通道")
        target_version = str(payload.get("target_version", "")).strip()
        auto_update = bool(payload.get("auto_update", True))
        if target_version and not VERSION_RE.fullmatch(target_version):
            raise HTTPException(status_code=400, detail="目标版本号不合法")
        with db() as conn:
            if target_version and not conn.execute(
                "SELECT 1 FROM agent_releases WHERE version = %s AND enabled = TRUE", (target_version,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="目标 agent 版本不存在")
            row = conn.execute(
                """
                UPDATE nodes SET agent_update_channel = %s, agent_auto_update = %s,
                    target_agent_version = %s, agent_update_status = 'idle', agent_update_error = ''
                WHERE id = %s RETURNING *
                """,
                (channel, auto_update, target_version, node_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="节点不存在")
            audit(conn, "admin", "configure-update", f"node:{node_id}", {"channel": channel, "auto_update": auto_update, "target_version": target_version})
            return {"ok": True}

    @app.get("/api/agent-updates/manifest")
    def update_manifest(request: Request, architecture: str = "amd64"):
        token, hostname = _agent_credentials(request)
        architecture = _clean(architecture, ARCHITECTURES, "架构")
        with db() as conn:
            node = verify_agent_node(conn, token, hostname)
            active_task = conn.execute(
                """
                SELECT 1 FROM node_tasks
                WHERE node_id = %s
                  AND status = 'claimed'
                  AND task_type != 'trigger_agent_update'
                  AND claimed_at >= %s
                LIMIT 1
                """,
                (node["id"], now_ts() - 300),
            ).fetchone()
            if active_task:
                return {"update_available": False, "current_version": node["agent_version"], "deferred_reason": "node task is running"}
            release = None
            if node["agent_auto_update"]:
                if node["target_agent_version"]:
                    release = conn.execute(
                        "SELECT * FROM agent_releases WHERE version = %s AND architecture = %s AND enabled = TRUE",
                        (node["target_agent_version"], architecture),
                    ).fetchone()
                else:
                    release = conn.execute(
                        """
                        SELECT * FROM agent_releases
                        WHERE channel = %s AND architecture = %s AND enabled = TRUE
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (node["agent_update_channel"], architecture),
                    ).fetchone()
            if not release or release["version"] == node["agent_version"]:
                return {"update_available": False, "current_version": node["agent_version"]}
            return {
                "update_available": True,
                "current_version": node["agent_version"],
                "version": release["version"],
                "sha256": release["sha256"],
                "size_bytes": release["size_bytes"],
                "download_url": f"/api/agent-updates/download/{release['version']}?architecture={architecture}",
            }

    @app.get("/api/agent-updates/download/{version}")
    def download_release(version: str, request: Request, architecture: str = "amd64"):
        token, hostname = _agent_credentials(request)
        architecture = _clean(architecture, ARCHITECTURES, "架构")
        with db() as conn:
            verify_agent_node(conn, token, hostname)
            release = conn.execute(
                "SELECT * FROM agent_releases WHERE version = %s AND architecture = %s AND enabled = TRUE",
                (version, architecture),
            ).fetchone()
        if not release:
            raise HTTPException(status_code=404, detail="agent 版本不存在")
        path = _release_path(release["file_name"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="agent 发布文件缺失")
        return FileResponse(path, media_type="application/octet-stream", filename="cluster-node-agent")

    @app.post("/api/agent-updates/report")
    async def report_update(request: Request):
        token, hostname = _agent_credentials(request)
        payload = await request.json()
        status = str(payload.get("status", "")).strip()
        if status not in UPDATE_STATUSES:
            raise HTTPException(status_code=400, detail="更新状态不合法")
        error = str(payload.get("error", ""))[:2000]
        with db() as conn:
            node = verify_agent_node(conn, token, hostname)
            conn.execute(
                "UPDATE nodes SET agent_update_status = %s, agent_update_error = %s, agent_update_at = %s WHERE id = %s",
                (status, error, now_ts(), node["id"]),
            )
        return {"ok": True}
