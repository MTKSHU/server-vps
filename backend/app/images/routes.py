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
    enqueue_node_task = deps["enqueue_node_task"]
    storage_root_for_node = deps["storage_root_for_node"]
    storage_image_base_name = deps["storage_image_base_name"]
    find_node_incus_image = deps["find_node_incus_image"]
    incus_image_import_payload = deps["incus_image_import_payload"]
    incus_image_push_payload = deps["incus_image_push_payload"]

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
        if incus_ref.startswith("local:"):
            raise HTTPException(status_code=400, detail="本地自建镜像（local:）请使用 copy-local-image 接口分发，不支持 pull-to-nodes")
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
    def copy_local_image_to_node(body: dict):
        """
        将本地自建 Incus 镜像分发到目标节点。

        - 若源节点已有导出文件（status='exported'）：直接下发 incus_image_import 任务，
          目标节点 agent rsync 直连源节点，管理端不参与数据传输。
        - 若尚无导出文件：下发 incus_image_export 任务（含 distribute_to_node_ids），
          导出完成后 agent 回调自动触发 incus_image_import。
        两种情况任务均写入 node_tasks，可在任务中心查看进度。
        """
        require_admin()
        target_node_id = body.get("target_node_id")
        image_ref = (body.get("image_ref") or "").strip()
        if not target_node_id or not image_ref:
            raise HTTPException(status_code=400, detail="target_node_id 和 image_ref 不能为空")

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

            # 优先使用任意节点上已导出的 storage_image_files（优先存储节点）
            existing_sf = conn.execute(
                """
                SELECT sif.*
                FROM storage_image_files sif
                JOIN nodes n ON n.id = sif.source_node_id
                WHERE sif.status = 'exported'
                  AND n.status = 'online'
                  AND (
                    sif.aliases = %s
                    OR sif.aliases LIKE %s
                    OR sif.aliases LIKE %s
                    OR sif.aliases LIKE %s
                  )
                ORDER BY
                  CASE n.node_type WHEN 'storage' THEN 1 WHEN 'mixed' THEN 2 ELSE 3 END,
                  sif.exported_at DESC NULLS LAST
                LIMIT 1
                """,
                (alias, f"{alias},%", f"%,{alias}", f"%,{alias},%"),
            ).fetchone()

            if existing_sf:
                sf_source = conn.execute(
                    "SELECT * FROM nodes WHERE id = %s", (existing_sf["source_node_id"],)
                ).fetchone()
                target_root = storage_root_for_node(conn, target["id"])
                task = enqueue_node_task(
                    conn,
                    target["id"],
                    None,
                    "incus_image_import",
                    incus_image_import_payload(
                        dict(existing_sf),
                        dict(sf_source),
                        dict(target),
                        f"{target_root}/incus-images/import-cache/{existing_sf['base_name']}",
                        alias,
                    ),
                )
                audit(conn, "admin", "copy-local-image", f"node:{target_node_id}",
                      {"alias": alias, "source_node": sf_source["hostname"], "path": "direct-import"})
                return {
                    "ok": True,
                    "task_ids": [task["id"]],
                    "source_node": sf_source["hostname"],
                    "target_node": target["hostname"],
                    "message": (
                        f"镜像导入任务已提交（任务 #{task['id']}）："
                        f"{sf_source['hostname']} → {target['hostname']}（{alias}）。"
                        f"可在任务中心查看进度。"
                    ),
                }

            # 无现成导出文件：在源节点导出 → 推送到存储节点备份 → 从存储节点分发
            incus_image = find_node_incus_image(conn, source["id"], alias)
            if not incus_image:
                raise HTTPException(status_code=404, detail=f"源节点 Incus 库存中没有镜像 '{alias}'")

            storage_node = conn.execute(
                """
                SELECT * FROM nodes
                WHERE status = 'online' AND node_type IN ('storage', 'mixed')
                ORDER BY CASE node_type WHEN 'storage' THEN 1 ELSE 2 END, hostname
                LIMIT 1
                """
            ).fetchone()
            if not storage_node:
                raise HTTPException(status_code=400, detail="没有可用的在线存储节点，无法备份导出镜像")

            compute_root = storage_root_for_node(conn, source["id"])
            storage_root_val = storage_root_for_node(conn, storage_node["id"])
            base_name = storage_image_base_name(incus_image["fingerprint"], alias)
            compute_export_dir = f"{compute_root}/incus-images/tmp-push/{base_name}"
            storage_export_dir = f"{storage_root_val}/incus-images/{base_name}"
            ts = now_ts()

            sf_row = conn.execute(
                """
                INSERT INTO storage_image_files (
                    source_node_id, owner_id, fingerprint, aliases, description, architecture,
                    export_dir, base_name, size_bytes, status, last_error, created_at, updated_at
                ) VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, 0, 'pending', '', %s, %s)
                ON CONFLICT (source_node_id, fingerprint) DO UPDATE SET
                    status = 'pending',
                    last_error = '',
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    storage_node["id"],          # 存储节点持有此文件
                    incus_image["fingerprint"],
                    incus_image["aliases"],
                    incus_image["description"],
                    incus_image["architecture"],
                    storage_export_dir,          # 最终落地路径在存储节点
                    base_name,
                    ts,
                    ts,
                ),
            ).fetchone()

            export_task = enqueue_node_task(
                conn,
                source["id"],
                None,
                "incus_image_export",
                {
                    "storage_image_file_id": sf_row["id"],
                    "image_ref": alias,
                    "alias": alias,
                    "export_dir": compute_export_dir,  # 先导出到计算节点本地临时目录
                    "base_name": base_name,
                    # 导出完成后由回调依次触发推送和分发
                    "push_to_storage": incus_image_push_payload(
                        dict(source),
                        dict(storage_node),
                        compute_export_dir,
                        storage_export_dir,
                        base_name,
                        sf_row["id"],
                        [target["id"]],
                    ),
                },
            )
            audit(conn, "admin", "copy-local-image", f"node:{target_node_id}",
                  {"alias": alias, "source_node": source["hostname"],
                   "storage_node": storage_node["hostname"], "path": "export+push+import"})
            return {
                "ok": True,
                "task_ids": [export_task["id"]],
                "source_node": source["hostname"],
                "target_node": target["hostname"],
                "message": (
                    f"镜像导出任务已提交（任务 #{export_task['id']}）：{source['hostname']}（{alias}）。"
                    f"导出→推送到存储节点→分发到 {target['hostname']} 将依次自动进行，可在任务中心查看进度。"
                ),
            }
