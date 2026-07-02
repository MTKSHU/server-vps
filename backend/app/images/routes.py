import os
import re
import shlex
import tempfile
import time
from typing import Any

import asyncssh
import httpx
from fastapi import BackgroundTasks, HTTPException
from psycopg.types.json import Jsonb

from ..schemas import ImageInput
from ..auth import require_admin
from ..config import SYNC_SSH_IDENTITY_FILE, SYNC_SSH_PORT, SYNC_SSH_USER

_ubuntu_remotes_cache: dict[str, Any] = {"data": None, "expires": 0}
_LXC_STREAMS_URL = "https://images.linuxcontainers.org/streams/v1/images.json"


def _ssh_kwargs(node: dict) -> dict:
    """从节点记录构建 asyncssh.connect 参数（复用平台 Sync SSH 密钥配置）。"""
    host = str(node.get("sync_ip") or node.get("ip") or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail=f"节点 {node.get('hostname')} 地址缺失")
    try:
        ssh_port = int(node.get("ssh_port") or SYNC_SSH_PORT)
    except (TypeError, ValueError):
        ssh_port = SYNC_SSH_PORT
    kwargs: dict = dict(
        host=host,
        port=ssh_port,
        username=str(node.get("ssh_user") or "").strip() or SYNC_SSH_USER,
        known_hosts=None,
    )
    _cluster_key = os.path.join(
        os.environ.get("AGENT_RELEASE_DIR", "/var/lib/cluster-agent-releases"),
        ".cluster_node_key",
    )
    if SYNC_SSH_IDENTITY_FILE and os.path.isfile(SYNC_SSH_IDENTITY_FILE):
        kwargs["client_keys"] = [SYNC_SSH_IDENTITY_FILE]
    elif os.path.isfile(_cluster_key):
        kwargs["client_keys"] = [_cluster_key]
    return kwargs


async def _ssh_copy_incus_image(source: dict, target: dict, alias: str) -> None:
    """
    通过管理端 SSH 通道，把 source 节点上的 Incus 镜像传输到 target 节点。

    流程：
      1. source: incus image export <alias> /tmp/xxx  → 生成 /tmp/xxx.tar.gz
      2. SFTP 下载到管理端临时文件
      3. source: 清理临时文件
      4. SFTP 上传到 target 节点 /tmp/xxx.tar.gz
      5. target: incus image import /tmp/xxx.tar.gz --alias <alias>
      6. target: 清理临时文件

    本函数作为 FastAPI BackgroundTask 运行，不阻塞 HTTP 请求。
    """
    safe = alias.replace("/", "-").replace(":", "-")
    remote_base = f"/tmp/incus-dist-{safe}-{os.getpid()}"
    remote_gz   = f"{remote_base}.tar.gz"
    local_tmp: str | None = None

    try:
        # ── Step 1-3: export + download from source ───────────────────────
        async with asyncssh.connect(**_ssh_kwargs(source)) as src:
            await src.run(
                f"incus image export {shlex.quote(alias)} {shlex.quote(remote_base)}",
                check=True,
            )
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
                local_tmp = tf.name
            async with src.start_sftp_client() as sftp:
                await sftp.get(remote_gz, local_tmp)
            await src.run(f"rm -f {shlex.quote(remote_gz)}")

        # ── Step 4-6: upload + import on target ───────────────────────────
        async with asyncssh.connect(**_ssh_kwargs(target)) as tgt:
            async with tgt.start_sftp_client() as sftp:
                await sftp.put(local_tmp, remote_gz)
            # 先删除同名别名（忽略错误），再导入
            await tgt.run(
                f"incus image delete {shlex.quote(alias)} 2>/dev/null || true; "
                f"incus image import {shlex.quote(remote_gz)} --alias {shlex.quote(alias)}",
                check=True,
            )
            await tgt.run(f"rm -f {shlex.quote(remote_gz)}")

        print(
            f"incus image copy {alias}: {source['hostname']} → {target['hostname']} OK",
            flush=True,
        )
    except Exception as exc:
        print(
            f"incus image copy {alias}: {source['hostname']} → {target['hostname']} FAILED: {exc}",
            flush=True,
        )
    finally:
        if local_tmp:
            try:
                os.unlink(local_tmp)
            except OSError:
                pass


def public_image(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "cuda_major": row["cuda_major"],
        "compatible_pools": row["compatible_pools"],
        "incus_ref": row["incus_ref"],
        "enabled": row["enabled"],
        "preferred": row["preferred"],
        "owner": row["owner"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def normalize_image_payload(payload: ImageInput) -> ImageInput:
    payload.id = payload.id.strip()
    payload.name = payload.name.strip()
    payload.incus_ref = payload.incus_ref.strip()
    payload.compatible_pools = ",".join(
        item.strip() for item in payload.compatible_pools.split(",") if item.strip()
    )
    payload.owner = payload.owner.strip() or "admin"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,80}", payload.id):
        raise HTTPException(status_code=400, detail="镜像 ID 只能包含字母、数字、点、下划线、冒号和连字符")
    if not payload.name:
        raise HTTPException(status_code=400, detail="镜像名称不能为空")
    if not payload.incus_ref:
        raise HTTPException(status_code=400, detail="Incus 镜像引用不能为空")
    if payload.cuda_major < 0 or payload.cuda_major > 99:
        raise HTTPException(status_code=400, detail="CUDA 主版本不合法")
    if not payload.compatible_pools:
        payload.compatible_pools = "legacy-pascal,modern-geforce,workstation,unknown"
    return payload


def register_image_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]
    audit = deps["audit"]

    from ..schemas import ImageCatalogOut, ImageOut

    @app.get("/api/images", response_model=list[ImageOut])
    def images():
        with db() as conn:
            rows = conn.execute("SELECT * FROM images WHERE enabled = TRUE ORDER BY preferred DESC, name").fetchall()
            return [public_image(row) for row in rows]

    @app.get("/api/image-catalog", response_model=ImageCatalogOut)
    def image_catalog():
        require_admin()
        with db() as conn:
            platform_images = conn.execute("SELECT * FROM images ORDER BY preferred DESC, enabled DESC, name").fetchall()
            incus_images = conn.execute(
                """
                SELECT nii.*, n.hostname, n.status AS node_status
                FROM node_incus_images nii
                JOIN nodes n ON n.id = nii.node_id
                ORDER BY n.hostname, nii.aliases, nii.description, nii.fingerprint
                """
            ).fetchall()
            return {
                "images": [public_image(row) for row in platform_images],
                "incus_images": [
                    {
                        "node_id": row["node_id"],
                        "node": row["hostname"],
                        "node_status": row["node_status"],
                        "fingerprint": row["fingerprint"],
                        "aliases": row["aliases"],
                        "description": row["description"],
                        "architecture": row["architecture"],
                        "updated_at": row["updated_at"],
                    }
                    for row in incus_images
                ],
            }

    @app.post("/api/images", status_code=201)
    def create_image(payload: ImageInput):
        require_admin()
        payload = normalize_image_payload(payload)
        ts = now_ts()
        with db() as conn:
            row = conn.execute(
                """
                INSERT INTO images (
                    id, name, cuda_major, compatible_pools, incus_ref, enabled, preferred, owner, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    cuda_major = EXCLUDED.cuda_major,
                    compatible_pools = EXCLUDED.compatible_pools,
                    incus_ref = EXCLUDED.incus_ref,
                    enabled = EXCLUDED.enabled,
                    preferred = EXCLUDED.preferred,
                    owner = EXCLUDED.owner,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    payload.id,
                    payload.name,
                    payload.cuda_major,
                    payload.compatible_pools,
                    payload.incus_ref,
                    payload.enabled,
                    payload.preferred,
                    payload.owner,
                    ts,
                    ts,
                ),
            ).fetchone()
            audit(conn, "admin", "upsert", f"image:{payload.id}", {"incus_ref": payload.incus_ref})
            return public_image(row)

    @app.put("/api/images/{image_id}")
    def update_image(image_id: str, payload: ImageInput):
        payload.id = image_id
        return create_image(payload)

    @app.delete("/api/images/{image_id}")
    def delete_image(image_id: str):
        require_admin()
        with db() as conn:
            in_use = conn.execute("SELECT 1 FROM containers WHERE image_id = %s LIMIT 1", (image_id,)).fetchone()
            if in_use:
                row = conn.execute(
                    "UPDATE images SET enabled = FALSE, preferred = FALSE, updated_at = %s WHERE id = %s RETURNING *",
                    (now_ts(), image_id),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="镜像不存在")
                audit(conn, "admin", "disable", f"image:{image_id}", {})
                return public_image(row)
            row = conn.execute("DELETE FROM images WHERE id = %s RETURNING *", (image_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="镜像不存在")
            audit(conn, "admin", "delete", f"image:{image_id}", {})
            return {"ok": True}

    @app.get("/api/image-catalog/ubuntu-remotes")
    def ubuntu_remote_images():
        require_admin()
        now = time.time()
        if _ubuntu_remotes_cache["data"] is not None and now < _ubuntu_remotes_cache["expires"]:
            return _ubuntu_remotes_cache["data"]
        try:
            resp = httpx.get(_LXC_STREAMS_URL, timeout=20, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"无法获取远程镜像列表: {exc}")
        products = data.get("products", {})
        result = []
        for key, val in products.items():
            if not key.lower().startswith("ubuntu:"):
                continue
            aliases_raw = [a.strip() for a in val.get("aliases", "").split(",") if a.strip()]
            # Extract version number from aliases like ubuntu/22.04
            version = ""
            for alias in aliases_raw:
                m = re.match(r"ubuntu/(\d+\.\d+)$", alias)
                if m:
                    version = m.group(1)
                    break
            # Primary incus ref: prefer ubuntu/XX.XX alias, else first alias
            primary_alias = next(
                (a for a in aliases_raw if re.match(r"ubuntu/\d+\.\d+$", a)),
                aliases_raw[0] if aliases_raw else key,
            )
            versions = val.get("versions", {})
            latest_serial = sorted(versions.keys())[-1] if versions else ""
            result.append({
                "key": key,
                "os": val.get("os", "Ubuntu"),
                "release": val.get("release", ""),
                "version": version,
                "arch": val.get("arch", ""),
                "variant": val.get("variant", "default"),
                "aliases": aliases_raw,
                "incus_ref": f"images:{primary_alias}",
                "latest_serial": latest_serial,
            })
        result.sort(key=lambda x: (x.get("version") or "0", x["arch"], x["variant"]), reverse=True)
        _ubuntu_remotes_cache["data"] = result
        _ubuntu_remotes_cache["expires"] = now + 3600
        return result

    @app.post("/api/image-catalog/pull-to-nodes", status_code=202)
    def pull_image_to_nodes(body: dict):
        require_admin()
        incus_ref = (body.get("incus_ref") or "").strip()
        if not incus_ref:
            raise HTTPException(status_code=400, detail="incus_ref 不能为空")
        target_node_id = body.get("node_id")  # 可选：指定单个节点
        alias = incus_ref.partition(":")[2] or incus_ref
        with db() as conn:
            if target_node_id:
                nodes = conn.execute(
                    "SELECT id FROM nodes WHERE id = %s AND status = 'online' AND node_type IN ('compute', 'mixed')",
                    (target_node_id,),
                ).fetchall()
                if not nodes:
                    raise HTTPException(status_code=409, detail="目标节点不在线或不是计算节点")
            else:
                nodes = conn.execute(
                    """
                    SELECT id FROM nodes
                    WHERE status = 'online' AND node_type IN ('compute', 'mixed')
                    ORDER BY id
                    """
                ).fetchall()
                if not nodes:
                    raise HTTPException(status_code=409, detail="当前没有在线的计算节点")
            task_ids = []
            ts = now_ts()
            for node in nodes:
                row = conn.execute(
                    """
                    INSERT INTO node_tasks (node_id, container_id, data_sync_task_id, task_type, payload, status, available_at, created_at, updated_at)
                    VALUES (%s, NULL, NULL, 'incus_image_pull', %s, 'pending', 0, %s, %s)
                    RETURNING id
                    """,
                    (node["id"], Jsonb({"image_ref": incus_ref, "alias": alias}), ts, ts),
                ).fetchone()
                task_ids.append(row["id"])
            audit(conn, "admin", "pull", f"remote_image:{incus_ref}", {"node_count": len(nodes)})
        return {"task_ids": task_ids, "node_count": len(nodes)}

    @app.post("/api/image-catalog/delete-node-image", status_code=202)
    def delete_node_image(body: dict):
        require_admin()
        node_id = body.get("node_id")
        image_ref = (body.get("image_ref") or "").strip()
        if not node_id or not image_ref:
            raise HTTPException(status_code=400, detail="node_id 和 image_ref 不能为空")
        with db() as conn:
            node = conn.execute(
                "SELECT id FROM nodes WHERE id = %s AND status = 'online'",
                (node_id,),
            ).fetchone()
            if not node:
                raise HTTPException(status_code=409, detail="目标节点不在线")
            ts = now_ts()
            row = conn.execute(
                """
                INSERT INTO node_tasks (node_id, container_id, data_sync_task_id, task_type, payload, status, available_at, created_at, updated_at)
                VALUES (%s, NULL, NULL, 'incus_delete_image', %s, 'pending', 0, %s, %s)
                RETURNING id
                """,
                (node_id, Jsonb({"image_ref": image_ref}), ts, ts),
            ).fetchone()
            audit(conn, "admin", "delete-node-image", f"node:{node_id}", {"image_ref": image_ref})
        return {"task_id": row["id"]}

    @app.post("/api/image-catalog/copy-local-image", status_code=202)
    async def copy_local_image_to_node(body: dict, background_tasks: BackgroundTasks):
        """
        将本地自建 Incus 镜像从拥有该镜像的节点复制到目标节点。

        与远程镜像（images:ubuntu/24.04）不同，本地镜像无法通过
        incus image copy local:xxx 跨节点复制，需要走：
          源节点 export → 管理端中转 → 目标节点 import
        本接口找到一个拥有该镜像的在线节点作为来源，通过 SSH 完成传输，
        操作在后台进行，立即返回。
        """
        require_admin()
        target_node_id = body.get("target_node_id")
        image_ref = (body.get("image_ref") or "").strip()
        if not target_node_id or not image_ref:
            raise HTTPException(status_code=400, detail="target_node_id 和 image_ref 不能为空")

        # 去掉 "local:" 前缀，得到 Incus 内部别名
        alias = image_ref.removeprefix("local:") if image_ref.startswith("local:") else image_ref

        with db() as conn:
            target = conn.execute(
                "SELECT * FROM nodes WHERE id = %s AND status = 'online'",
                (target_node_id,),
            ).fetchone()
            if not target:
                raise HTTPException(status_code=409, detail="目标节点不在线")

            # 在其他在线节点中找到持有该别名的来源节点
            source = conn.execute(
                """
                SELECT DISTINCT n.*
                FROM nodes n
                JOIN node_incus_images nii ON nii.node_id = n.id
                WHERE n.status = 'online'
                  AND n.id != %s
                  AND (
                    nii.aliases = %s
                    OR nii.aliases LIKE %s
                    OR nii.aliases LIKE %s
                    OR nii.aliases LIKE %s
                  )
                ORDER BY n.id
                LIMIT 1
                """,
                (target_node_id, alias, f"{alias},%", f"%,{alias}", f"%,{alias},%"),
            ).fetchone()
            if not source:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"找不到拥有镜像 '{alias}' 的其他在线节点。"
                        f"请先在某个计算节点上导入该镜像（例如用 build-incus-image.sh --node），"
                        f"再尝试分发到其他节点。"
                    ),
                )
            source_dict = dict(source)
            target_dict = dict(target)
            audit(conn, "admin", "copy-local-image", f"node:{target_node_id}",
                  {"alias": alias, "source_node": source["hostname"]})

        background_tasks.add_task(_ssh_copy_incus_image, source_dict, target_dict, alias)
        msg = (
            "镜像传输已在后台启动：" + source_dict["hostname"]
            + " → " + target_dict["hostname"]
            + "（" + alias + "）。传输完成后请刷新「节点本地镜像」页面确认。"
        )
        return {
            "ok": True,
            "alias": alias,
            "source_node": source_dict["hostname"],
            "target_node": target_dict["hostname"],
            "message": msg,
        }
