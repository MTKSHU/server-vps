import re
from typing import Any

from fastapi import HTTPException, Request

from .auth import create_session, token_hash, verify_password, hash_password, role_for_group
from .platform_settings import get_platform_settings
from .schemas import LoginInput, PasswordChangeInput, RegisterInput
from .sso.registry import load_providers


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in user.items() if key not in ("password_hash",)}


def register_auth_routes(app, deps):
    db, now_ts, current_user = deps["db"], deps["now_ts"], deps["current_user"]
    ensure_user_zfs_dataset_task = deps.get("ensure_user_zfs_dataset_task")

    def auth_config(conn) -> dict[str, Any]:
        settings = get_platform_settings(conn)
        sso_enabled = bool(load_providers(settings))
        registration_mode = "disabled"
        if settings["platform_registration_enabled"]:
            registration_mode = "platform"
        elif sso_enabled and settings["sso_registration_enabled"]:
            registration_mode = "sso"
        return {
            "local_login_enabled": settings["local_login_enabled"],
            "sso_login_enabled": sso_enabled,
            "registration_enabled": registration_mode != "disabled",
            "registration_mode": registration_mode,
            "default_register_group": settings["platform_registration_default_group"],
            "platform_registration_auto_enable": settings["platform_registration_auto_enable"],
        }

    @app.get("/api/auth/config")
    def config():
        with db() as conn:
            return auth_config(conn)

    @app.post("/api/auth/login")
    def login(payload: LoginInput):
        with db() as conn:
            settings = get_platform_settings(conn)
            if not settings["local_login_enabled"]:
                raise HTTPException(status_code=403, detail="平台账号登录未启用")
            user = conn.execute("SELECT * FROM users WHERE username=%s", (payload.username.strip(),)).fetchone()
            if not user or not user["enabled"] or not verify_password(payload.password, user["password_hash"]):
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            token, expires_at = create_session(conn, user["id"], now_ts())
            return {"token": token, "expires_at": expires_at, "user": public_user(user)}

    @app.post("/api/auth/register", status_code=201)
    def register(payload: RegisterInput):
        username = payload.username.strip().lower()
        email = payload.email.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,31}", username):
            raise HTTPException(status_code=400, detail="用户名格式不合法")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise HTTPException(status_code=400, detail="邮箱格式不合法")
        if len(payload.password) < 10:
            raise HTTPException(status_code=400, detail="密码至少需要 10 个字符")
        ts = now_ts()
        with db() as conn:
            settings = get_platform_settings(conn)
            if not settings["platform_registration_enabled"]:
                raise HTTPException(status_code=403, detail="平台注册未启用")
            group_name = settings["platform_registration_default_group"]
            role = role_for_group(group_name)
            if conn.execute("SELECT 1 FROM users WHERE username=%s", (username,)).fetchone():
                raise HTTPException(status_code=409, detail="用户名已存在")
            if conn.execute("SELECT 1 FROM users WHERE LOWER(email)=LOWER(%s) AND email != ''", (email,)).fetchone():
                raise HTTPException(status_code=409, detail="邮箱已被注册")
            profile = conn.execute("SELECT * FROM quota_profiles WHERE group_name=%s", (group_name,)).fetchone()
            if not profile:
                raise HTTPException(status_code=500, detail="默认注册分组不存在")
            user = conn.execute(
                """
                INSERT INTO users (
                    username, display_name, role, ssh_key, email, group_name,
                    password_hash, enabled, created_at
                ) VALUES (%s,%s,%s,'',%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    username,
                    username,
                    role,
                    email,
                    group_name,
                    hash_password(payload.password),
                    settings["platform_registration_auto_enable"],
                    ts,
                ),
            ).fetchone()
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
                """
                INSERT INTO user_data_policies (user_id, home_path, created_at, updated_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user["id"], f"/data/users/{username}", ts, ts),
            )
            if ensure_user_zfs_dataset_task and user["enabled"]:
                ensure_user_zfs_dataset_task(conn, user["id"], "platform-register")
            return {
                "ok": True,
                "enabled": bool(user["enabled"]),
                "auto_enable": settings["platform_registration_auto_enable"],
                "user": public_user(user),
            }

    @app.post("/api/auth/logout")
    def logout(request: Request):
        raw = request.headers.get("authorization", "")
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
        with db() as conn:
            if token:
                conn.execute("DELETE FROM auth_sessions WHERE token_hash=%s", (token_hash(token),))
        return {"ok": True}

    @app.put("/api/me/password")
    def change_password(payload: PasswordChangeInput):
        if len(payload.new_password) < 10:
            raise HTTPException(status_code=400, detail="新密码至少需要 10 个字符")
        with db() as conn:
            user = current_user(conn)
            stored = conn.execute("SELECT password_hash FROM users WHERE id=%s", (user["id"],)).fetchone()
            if not verify_password(payload.current_password, stored["password_hash"]):
                raise HTTPException(status_code=400, detail="当前密码错误")
            conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(payload.new_password), user["id"]))
            conn.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user["id"],))
        return {"ok": True}
