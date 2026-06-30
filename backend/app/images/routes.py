import re
import time
from typing import Any

import httpx
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..schemas import ImageInput
from ..auth import require_admin

_ubuntu_remotes_cache: dict[str, Any] = {"data": None, "expires": 0}
_LXC_STREAMS_URL = "https://images.linuxcontainers.org/streams/v1/images.json"


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

    @app.get("/api/images")
    def images():
        with db() as conn:
            rows = conn.execute("SELECT * FROM images WHERE enabled = TRUE ORDER BY preferred DESC, name").fetchall()
            return [public_image(row) for row in rows]

    @app.get("/api/image-catalog")
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
