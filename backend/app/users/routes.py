import re
from typing import Any

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from ..core import audit
from ..platform_settings import get_platform_settings
from ..schemas import QuotaProfileInput, SshKeyInput, UserPreferenceInput, UserProfileInput, UserUpsertInput
from ..auth import hash_password, require_admin, role_for_group
from ..containers.ports import managed_ssh_keys
from ..nodes.services import allowed_node_ids_for_user
from ..sso.casdoor_mgmt import fetch_pending_casdoor_users


def validate_preference_key(key: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,80}", key):
        raise HTTPException(status_code=400, detail="偏好设置 key 不合法")
    return key


def normalize_node_ids(node_ids: list[int]) -> list[int]:
    return list(dict.fromkeys(node_id for node_id in node_ids if node_id > 0))


def validate_node_ids(conn, node_ids: list[int]):
    if not node_ids:
        return
    rows = conn.execute("SELECT id FROM nodes WHERE id = ANY(%s::bigint[])", (node_ids,)).fetchall()
    found = {row["id"] for row in rows}
    missing = [node_id for node_id in node_ids if node_id not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"节点不存在：{missing[0]}")


def profile_node_ids(conn, group_name: str) -> list[int]:
    return [
        row["node_id"]
        for row in conn.execute(
            "SELECT node_id FROM quota_profile_node_access WHERE group_name=%s ORDER BY node_id",
            (group_name,),
        ).fetchall()
    ]


def user_node_ids(conn, user_id: int) -> list[int]:
    return [
        row["node_id"]
        for row in conn.execute(
            "SELECT node_id FROM user_node_access WHERE user_id=%s ORDER BY node_id",
            (user_id,),
        ).fetchall()
    ]


def replace_profile_node_ids(conn, group_name: str, node_ids: list[int], ts: int):
    node_ids = normalize_node_ids(node_ids)
    validate_node_ids(conn, node_ids)
    conn.execute("DELETE FROM quota_profile_node_access WHERE group_name=%s", (group_name,))
    for node_id in node_ids:
        conn.execute(
            "INSERT INTO quota_profile_node_access (group_name, node_id, created_at) VALUES (%s,%s,%s)",
            (group_name, node_id, ts),
        )


def replace_user_node_ids(conn, user_id: int, node_ids: list[int], ts: int):
    node_ids = normalize_node_ids(node_ids)
    validate_node_ids(conn, node_ids)
    conn.execute("DELETE FROM user_node_access WHERE user_id=%s", (user_id,))
    for node_id in node_ids:
        conn.execute(
            "INSERT INTO user_node_access (user_id, node_id, created_at) VALUES (%s,%s,%s)",
            (user_id, node_id, ts),
        )


def register_user_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]
    current_user = deps["current_user"]
    usage_for_user = deps["usage_for_user"]
    enqueue_node_task = deps["enqueue_node_task"]
    ensure_user_zfs_dataset_task = deps.get("ensure_user_zfs_dataset_task")

    @app.get("/api/health")
    def health():
        return {"ok": True, "database": "postgresql"}

    @app.get("/api/me")
    def me():
        with db() as conn:
            user = current_user(conn)
            user["quota"] = conn.execute("SELECT * FROM quotas WHERE user_id = %s", (user["id"],)).fetchone()
            user["usage"] = usage_for_user(conn, user["id"])
            allowed_node_ids = allowed_node_ids_for_user(conn, user)
            user["allowed_node_ids"] = sorted(allowed_node_ids) if allowed_node_ids is not None else None
            user.pop("password_hash", None)
            return user

    @app.put("/api/me")
    def update_profile(payload: UserProfileInput):
        display_name = payload.display_name.strip()
        if not display_name:
            raise HTTPException(status_code=400, detail="用户名称不能为空")
        with db() as conn:
            user = current_user(conn)
            updated = conn.execute(
                "UPDATE users SET display_name=%s, phone=%s WHERE id=%s RETURNING *",
                (display_name, payload.phone.strip(), user["id"]),
            ).fetchone()
            return {k: v for k, v in updated.items() if k != "password_hash"}

    @app.get("/api/me/ssh-keys")
    def list_ssh_keys():
        with db() as conn:
            user = current_user(conn)
            return conn.execute(
                "SELECT id, label, public_key, expires_at, created_at FROM user_ssh_keys WHERE user_id=%s ORDER BY created_at",
                (user["id"],),
            ).fetchall()

    @app.post("/api/me/ssh-keys", status_code=201)
    def add_ssh_key(payload: SshKeyInput):
        key = payload.public_key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="SSH 公钥不能为空")
        parts = key.split()
        if len(parts) < 2 or not parts[0].startswith("ssh-") and parts[0] not in ("ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"):
            raise HTTPException(status_code=400, detail="SSH 公钥格式不合法")
        ts = now_ts()
        with db() as conn:
            user = current_user(conn)
            row = conn.execute(
                "INSERT INTO user_ssh_keys (user_id, label, public_key, expires_at, created_at) VALUES (%s,%s,%s,%s,%s) RETURNING id, label, public_key, expires_at, created_at",
                (user["id"], payload.label.strip()[:100], key, payload.expires_at, ts),
            ).fetchone()
            return row

    @app.delete("/api/me/ssh-keys/{key_id}", status_code=204)
    def delete_ssh_key(key_id: int):
        with db() as conn:
            user = current_user(conn)
            deleted = conn.execute(
                "DELETE FROM user_ssh_keys WHERE id=%s AND user_id=%s RETURNING id",
                (key_id, user["id"]),
            ).fetchone()
            if not deleted:
                raise HTTPException(status_code=404, detail="SSH 公钥不存在")

    @app.post("/api/me/ssh-keys/sync-to-containers", status_code=202)
    def sync_ssh_keys_to_containers():
        with db() as conn:
            user = current_user(conn)
            ts_now = now_ts()
            valid_keys = conn.execute(
                "SELECT public_key FROM user_ssh_keys WHERE user_id=%s AND (expires_at=0 OR expires_at>%s) ORDER BY created_at",
                (user["id"], ts_now),
            ).fetchall()
            if valid_keys:
                keys_content = managed_ssh_keys("\n".join(r["public_key"] for r in valid_keys))
            elif user.get("ssh_key", "").strip():
                keys_content = managed_ssh_keys(user["ssh_key"])
            else:
                raise HTTPException(status_code=400, detail="没有有效的 SSH 公钥可以同步")
            containers = conn.execute(
                """
                SELECT id, name, node_id, ssh_username, mounts, status
                FROM containers
                WHERE owner_id=%s
                  AND status NOT IN ('deleting')
                ORDER BY id
                """,
                (user["id"],),
            ).fetchall()
            if not containers:
                raise HTTPException(status_code=400, detail="没有可同步的容器")
            syncable_statuses = {"running", "stopped"}
            task_ids = []
            for container in containers:
                conn.execute(
                    "UPDATE containers SET ssh_key=%s, updated_at=%s WHERE id=%s",
                    (keys_content, ts_now, container["id"]),
                )
                if container["status"] not in syncable_statuses:
                    continue
                task = enqueue_node_task(
                    conn,
                    container["node_id"],
                    container["id"],
                    "incus_sync_ssh_keys",
                    {
                        "container_id": container["id"],
                        "name": container["name"],
                        "ssh_username": container["ssh_username"] or "ubuntu",
                        "ssh_key": keys_content,
                        "mounts": container["mounts"] or [],
                    },
                )
                conn.execute(
                    "UPDATE containers SET access_status = 'pending', access_error = '', updated_at = %s WHERE id = %s",
                    (ts_now, container["id"]),
                )
                task_ids.append(task["id"])
            return {"task_ids": task_ids, "container_count": len(containers)}

    @app.get("/api/me/preferences/{key}")
    def get_user_preference(key: str):
        key = validate_preference_key(key)
        with db() as conn:
            user = current_user(conn)
            row = conn.execute(
                "SELECT pref_key AS key, value, updated_at FROM user_preferences WHERE user_id = %s AND pref_key = %s",
                (user["id"], key),
            ).fetchone()
            return row or {"key": key, "value": {}, "updated_at": 0}

    @app.put("/api/me/preferences/{key}")
    def update_user_preference(key: str, payload: UserPreferenceInput):
        key = validate_preference_key(key)
        ts = now_ts()
        with db() as conn:
            user = current_user(conn)
            row = conn.execute(
                """
                INSERT INTO user_preferences (user_id, pref_key, value, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, pref_key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = EXCLUDED.updated_at
                RETURNING pref_key AS key, value, updated_at
                """,
                (user["id"], key, Jsonb(payload.value), ts, ts),
            ).fetchone()
            return row

    @app.get("/api/users")
    def users():
        require_admin()
        with db() as conn:
            settings = get_platform_settings(conn)
            platform_users = conn.execute(
                """
                SELECT u.id, u.username, u.display_name, u.phone, u.email,
                       u.role, u.group_name, u.enabled, u.ssh_key, u.created_at,
                       q.cpu_cores, q.memory_gb, q.disk_gb,
                       q.container_disk_limit_gb, q.storage_quota_gb,
                       q.gpu_count, q.container_count,
                       FALSE AS pending_sso, NULL::text AS casdoor_id
                FROM users u LEFT JOIN quotas q ON q.user_id = u.id
                WHERE u.username NOT LIKE %s ESCAPE '\'
                ORDER BY u.username
                """,
                ("\\_removed\\_%",),
            ).fetchall()
            linked_subjects = {
                row["subject"]
                for row in conn.execute(
                    "SELECT subject FROM user_identities WHERE provider LIKE 'oidc:casdoor%%'"
                ).fetchall()
            }
            sso_default_group = settings["sso_default_group"]

        casdoor_pending = fetch_pending_casdoor_users(linked_subjects, settings)

        result = []
        with db() as conn:
            for u in platform_users:
                item = dict(u)
                item["allowed_node_ids"] = user_node_ids(conn, item["id"])
                result.append(item)
        profile_defaults = {
            "cpu_cores": None,
            "memory_gb": None,
            "disk_gb": None,
            "container_disk_limit_gb": None,
            "storage_quota_gb": None,
            "gpu_count": None,
            "container_count": None,
        }
        for cu in casdoor_pending:
            result.append({
                "id": None,
                "username": cu["username"],
                "display_name": cu["display_name"],
                "phone": "",
                "email": cu["email"],
                "role": "member",
                "group_name": sso_default_group,
                "enabled": False,
                "ssh_key": "",
                "created_at": 0,
                **profile_defaults,
                "allowed_node_ids": [],
                "pending_sso": True,
                "casdoor_id": cu["casdoor_id"],
            })
        return result

    @app.get("/api/quota-profiles")
    def quota_profiles():
        require_admin()
        with db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM quota_profiles
                ORDER BY CASE group_name
                    WHEN 'platform_admin' THEN 1
                    WHEN 'admin' THEN 2
                    WHEN 'member' THEN 3
                    WHEN 'guest' THEN 4
                    ELSE 99
                END, group_name
                """
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["allowed_node_ids"] = profile_node_ids(conn, item["group_name"])
                result.append(item)
            return result

    @app.put("/api/quota-profiles/{group_name}")
    def update_quota_profile(group_name: str, payload: QuotaProfileInput):
        require_admin()
        if group_name not in ("platform_admin", "admin", "member", "guest"):
            raise HTTPException(status_code=400, detail="用户分组不合法")
        values = [
            payload.cpu_cores,
            payload.memory_gb,
            payload.disk_gb,
            payload.container_disk_limit_gb,
            payload.storage_quota_gb,
            payload.gpu_count,
            payload.container_count,
        ]
        if any(value < 0 for value in values):
            raise HTTPException(status_code=400, detail="配额不能为负数")
        with db() as conn:
            ts = now_ts()
            row = conn.execute(
                """
                INSERT INTO quota_profiles (
                    group_name, role, cpu_cores, memory_gb, disk_gb,
                    container_disk_limit_gb, storage_quota_gb, gpu_count, container_count, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (group_name) DO UPDATE SET role=EXCLUDED.role, cpu_cores=EXCLUDED.cpu_cores,
                  memory_gb=EXCLUDED.memory_gb, disk_gb=EXCLUDED.disk_gb,
                  container_disk_limit_gb=EXCLUDED.container_disk_limit_gb,
                  storage_quota_gb=EXCLUDED.storage_quota_gb,
                  gpu_count=EXCLUDED.gpu_count, container_count=EXCLUDED.container_count,
                  updated_at=EXCLUDED.updated_at RETURNING *
                """,
                (group_name, role_for_group(group_name), *values, ts),
            ).fetchone()
            replace_profile_node_ids(conn, group_name, payload.allowed_node_ids, ts)
            result = dict(row)
            result["allowed_node_ids"] = profile_node_ids(conn, group_name)
            return result

    def save_user(payload: UserUpsertInput, user_id: int | None = None):
        actor = require_admin()
        payload.username = payload.username.strip().lower()
        payload.display_name = payload.display_name.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,31}", payload.username):
            raise HTTPException(status_code=400, detail="用户名格式不合法")
        if not payload.display_name:
            raise HTTPException(status_code=400, detail="用户名称不能为空")
        if payload.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", payload.email.strip()):
            raise HTTPException(status_code=400, detail="联系邮箱格式不合法")
        if user_id is None and len(payload.password) < 10:
            raise HTTPException(status_code=400, detail="新用户初始密码至少需要 10 个字符")
        with db() as conn:
            profile = conn.execute("SELECT * FROM quota_profiles WHERE group_name=%s", (payload.group_name,)).fetchone()
            if not profile:
                raise HTTPException(status_code=400, detail="配额分组不存在")
            role = role_for_group(payload.group_name)
            ts = now_ts()
            if user_id is None:
                existing = None
                user = conn.execute(
                    """INSERT INTO users (username,display_name,role,ssh_key,phone,email,group_name,password_hash,enabled,created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (payload.username, payload.display_name, role, payload.ssh_key.strip(), payload.phone.strip(),
                     payload.email.strip(), payload.group_name, hash_password(payload.password), payload.enabled, ts),
                ).fetchone()
            else:
                existing = conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
                if not existing:
                    raise HTTPException(status_code=404, detail="用户不存在")
                if existing["id"] == actor["id"] and (not payload.enabled or role != "admin"):
                    raise HTTPException(status_code=400, detail="不能禁用自己或移除自己的管理员角色")
                password_sql = ", password_hash=%s" if payload.password else ""
                params = [payload.username, payload.display_name, role, payload.ssh_key.strip(), payload.phone.strip(),
                          payload.email.strip(), payload.group_name, payload.enabled]
                if payload.password:
                    if len(payload.password) < 10:
                        raise HTTPException(status_code=400, detail="密码至少需要 10 个字符")
                    params.append(hash_password(payload.password))
                params.append(user_id)
                user = conn.execute(
                    f"UPDATE users SET username=%s,display_name=%s,role=%s,ssh_key=%s,phone=%s,email=%s,group_name=%s,enabled=%s{password_sql} WHERE id=%s RETURNING *",
                    params,
                ).fetchone()
            quota_values = (
                payload.cpu_cores if payload.cpu_cores is not None else profile["cpu_cores"],
                payload.memory_gb if payload.memory_gb is not None else profile["memory_gb"],
                payload.disk_gb if payload.disk_gb is not None else profile["disk_gb"],
                payload.container_disk_limit_gb if payload.container_disk_limit_gb is not None else profile["container_disk_limit_gb"],
                payload.storage_quota_gb if payload.storage_quota_gb is not None else profile["storage_quota_gb"],
                payload.gpu_count if payload.gpu_count is not None else profile["gpu_count"],
                payload.container_count if payload.container_count is not None else profile["container_count"],
            )
            if any(value < 0 for value in quota_values):
                raise HTTPException(status_code=400, detail="配额不能为负数")
            conn.execute(
                """
                INSERT INTO quotas (
                    user_id, cpu_cores, memory_gb, disk_gb,
                    container_disk_limit_gb, storage_quota_gb, gpu_count, container_count
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                    cpu_cores=EXCLUDED.cpu_cores,
                    memory_gb=EXCLUDED.memory_gb,
                    disk_gb=EXCLUDED.disk_gb,
                    container_disk_limit_gb=EXCLUDED.container_disk_limit_gb,
                    storage_quota_gb=EXCLUDED.storage_quota_gb,
                    gpu_count=EXCLUDED.gpu_count,
                    container_count=EXCLUDED.container_count
                """,
                (user["id"], *quota_values),
            )
            replace_user_node_ids(conn, user["id"], payload.allowed_node_ids, ts)
            conn.execute("INSERT INTO user_data_policies (user_id,home_path,created_at,updated_at) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING", (user["id"], f"/data/users/{user['username']}", ts, ts))
            if ensure_user_zfs_dataset_task and user["enabled"] and (user_id is None or not (existing or {}).get("enabled") or payload.storage_quota_gb is not None):
                ensure_user_zfs_dataset_task(conn, user["id"], "user-save")
            return {
                **{key: value for key, value in user.items() if key != "password_hash"},
                **dict(zip(
                    (
                        "cpu_cores", "memory_gb", "disk_gb",
                        "container_disk_limit_gb", "storage_quota_gb",
                        "gpu_count", "container_count",
                    ),
                    quota_values,
                )),
                "allowed_node_ids": user_node_ids(conn, user["id"]),
            }

    @app.post("/api/users", status_code=201)
    def create_user(payload: UserUpsertInput):
        return save_user(payload)

    @app.put("/api/users/{user_id}")
    def update_user(user_id: int, payload: UserUpsertInput):
        return save_user(payload, user_id)

    class ApproveSsoInput(BaseModel):
        casdoor_id: str
        username: str
        display_name: str
        email: str = ""
        group_name: str = "member"

    @app.post("/api/users/approve-sso", status_code=201)
    def approve_sso_user(payload: ApproveSsoInput):
        """
        预批准一个已在 Casdoor 注册但从未登录过平台的用户。
        直接创建平台账号（enabled=True）并写入 user_identities 关联，
        之后该用户 SSO 登录时可直接进入平台。
        """
        require_admin()
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", payload.casdoor_id):
            raise HTTPException(status_code=400, detail="无效的 casdoor_id 格式")
        payload.username = payload.username.strip().lower()
        payload.display_name = payload.display_name.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,31}", payload.username):
            raise HTTPException(status_code=400, detail="用户名格式不合法")
        if not payload.display_name:
            raise HTTPException(status_code=400, detail="用户名称不能为空")
        if payload.group_name not in ("platform_admin", "admin", "member", "guest"):
            raise HTTPException(status_code=400, detail="用户分组不合法")

        ts = now_ts()
        with db() as conn:
            existing = conn.execute(
                "SELECT user_id FROM user_identities WHERE subject=%s AND provider LIKE 'oidc:casdoor%%'",
                (payload.casdoor_id,),
            ).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="该 Casdoor 用户已关联平台账号")

            base = payload.username
            candidate = base
            suffix = 2
            while conn.execute("SELECT 1 FROM users WHERE username=%s", (candidate,)).fetchone():
                candidate = f"{base[:28]}_{suffix}"
                suffix += 1
            username = candidate

            profile = conn.execute(
                "SELECT * FROM quota_profiles WHERE group_name=%s", (payload.group_name,)
            ).fetchone()
            role = role_for_group(payload.group_name)

            user = conn.execute(
                """INSERT INTO users
                       (username, display_name, role, ssh_key, external_id, email,
                        group_name, password_hash, enabled, created_at)
                   VALUES (%s,%s,%s,'','',%s,%s,'',TRUE,%s) RETURNING *""",
                (username, payload.display_name, role, payload.email.strip(), payload.group_name, ts),
            ).fetchone()

            if profile:
                conn.execute(
                    """
                    INSERT INTO quotas (
                        user_id, cpu_cores, memory_gb, disk_gb,
                        container_disk_limit_gb, storage_quota_gb, gpu_count, container_count
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        user["id"],
                        profile["cpu_cores"],
                        profile["memory_gb"],
                        profile["disk_gb"],
                        profile["container_disk_limit_gb"],
                        profile["storage_quota_gb"],
                        profile["gpu_count"],
                        profile["container_count"],
                    ),
                )
            conn.execute(
                "INSERT INTO user_data_policies (user_id,home_path,created_at,updated_at) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",
                (user["id"], f"/data/users/{username}", ts, ts),
            )
            ensure_user_zfs_dataset_task = deps.get("ensure_user_zfs_dataset_task")
            if ensure_user_zfs_dataset_task:
                ensure_user_zfs_dataset_task(conn, user["id"], "sso-approve")
            conn.execute(
                "INSERT INTO user_identities (user_id, provider, subject, created_at) VALUES (%s,'oidc:casdoor',%s,%s)",
                (user["id"], payload.casdoor_id, ts),
            )
            return {k: v for k, v in user.items() if k != "password_hash"}

    @app.delete("/api/users/{user_id}", status_code=204)
    def remove_user(user_id: int):
        actor = require_admin()
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="用户不存在")
            if user["id"] == actor["id"]:
                raise HTTPException(status_code=400, detail="不能移除自己")
            # 查找管理员账号（转移资源）
            admin_user = conn.execute(
                "SELECT id FROM users WHERE role='admin' AND id != %s ORDER BY id LIMIT 1", (user_id,)
            ).fetchone()
            admin_id = admin_user["id"] if admin_user else None
            ts = now_ts()
            if admin_id:
                # 容器、镜像所有权转移给管理员
                conn.execute("UPDATE containers SET owner_id=%s, updated_at=%s WHERE owner_id=%s AND status NOT IN ('deleting')", (admin_id, ts, user_id))
                conn.execute("UPDATE storage_image_files SET owner_id=%s WHERE owner_id=%s", (admin_id, user_id))
            # 禁用用户（软删除：保留记录，断绝登录；改名避免用户名冲突）
            conn.execute(
                "UPDATE users SET enabled=FALSE, username=%s WHERE id=%s",
                (f"_removed_{user['username']}_{user_id}", user_id),
            )
            conn.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user_id,))
            audit(conn, actor["username"], "remove-user", f"user:{user_id}",
                  {"username": user["username"], "transferred_to_admin": admin_id})
