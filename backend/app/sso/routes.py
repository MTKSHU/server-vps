"""SSO 统一认证路由。

端点：
  GET  /api/auth/sso/providers            → 列出已配置的 Provider（公开）
  GET  /api/auth/sso/start/{provider_id}  → 发起 SSO 登录，302 跳转至 IdP（公开）
  POST /api/auth/sso/callback             → 处理 IdP 回调，返回 session token（公开）

认证流程（以 OIDC 为例）：
  1. 前端调用 /api/auth/sso/start/{id}?return_to=/login/callback → 后端生成 state
     存入 sso_states 表，重定向到 IdP
  2. IdP 认证完成后重定向到平台设置中的 /login/callback?code=...&state=...
  3. 前端 LoginCallback.vue 读取 URL 参数，POST 到 /api/auth/sso/callback
  4. 后端验证 state、向 IdP 换取身份、查找或创建用户、返回 token

CAS 流程类似，IdP 回调参数为 ticket 而非 code。
"""
import re
import secrets
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..auth import create_session, role_for_group
from ..platform_settings import get_platform_settings
from .registry import load_providers


def _callback_url(settings: dict[str, Any]) -> str:
    """前端 SSO 回调页面的完整 URL，IdP 将把用户重定向至此处。"""
    base = settings["sso_callback_base_url"].rstrip("/")
    return f"{base}/login/callback"


# ── 用户名生成辅助 ─────────────────────────────────────────────────────────────
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,31}$")


def _sanitize_username(raw: str) -> str:
    """将任意字符串规范化为合法用户名（不保证唯一）。"""
    s = raw.lower().strip()
    s = re.sub(r"[^a-z0-9_.-]", "", s)
    if not s or not s[0].isalpha():
        s = "u" + s
    return s[:32] if len(s) >= 3 else s.ljust(3, "0")


def _unique_username(conn, base: str) -> str:
    """在 base 上附加数字后缀，直到找到未被占用的用户名。"""
    candidate = base
    suffix = 2
    while conn.execute("SELECT 1 FROM users WHERE username=%s", (candidate,)).fetchone():
        candidate = f"{base[:28]}_{suffix}"
        suffix += 1
    return candidate


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in user.items() if k not in ("password_hash",)}


# ── 自动创建 SSO 用户 ──────────────────────────────────────────────────────────
def _create_sso_user(conn, identity, provider_id: str, ts: int, settings: dict[str, Any], ensure_user_zfs_dataset_task=None) -> dict[str, Any]:
    """根据外部身份自动创建本地用户账号，并写入 user_identities 关联。"""
    # 生成唯一用户名
    hint = identity.username_hint or identity.email.split("@")[0] or identity.subject
    base = _sanitize_username(hint)
    username = _unique_username(conn, base)

    display_name = (identity.display_name or username)[:80]
    email = identity.email[:120] if identity.email else ""
    staff_id = identity.staff_id[:40] if identity.staff_id else ""
    group_name = settings["sso_default_group"]
    enabled = settings["sso_auto_enable_new_users"]

    # 取配额模板
    profile = conn.execute(
        "SELECT * FROM quota_profiles WHERE group_name=%s", (group_name,)
    ).fetchone()

    role = role_for_group(group_name)
    user = conn.execute(
        """INSERT INTO users
               (username, display_name, role, ssh_key, external_id, email,
                group_name, password_hash, enabled, created_at)
           VALUES (%s,%s,%s,'',%s,%s,%s,'',%s,%s) RETURNING *""",
        (username, display_name, role, staff_id, email, group_name, enabled, ts),
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
        "INSERT INTO user_data_policies (user_id,home_path,created_at,updated_at) VALUES (%s,%s,%s,%s)",
        (user["id"], f"/data/users/{username}", ts, ts),
    )
    conn.execute(
        "INSERT INTO user_identities (user_id, provider, subject, created_at) VALUES (%s,%s,%s,%s)",
        (user["id"], provider_id, identity.subject, ts),
    )
    if enabled and ensure_user_zfs_dataset_task:
        ensure_user_zfs_dataset_task(conn, user["id"], "sso-create")
    return user


# ── Pydantic Schema ────────────────────────────────────────────────────────────
class SSOCallbackInput(BaseModel):
    state: str
    code: str = ""    # OIDC Authorization Code Flow
    ticket: str = ""  # CAS Service Ticket


# ── 路由注册 ───────────────────────────────────────────────────────────────────
def register_sso_routes(app, deps):
    db, now_ts = deps["db"], deps["now_ts"]
    ensure_user_zfs_dataset_task = deps.get("ensure_user_zfs_dataset_task")

    @app.get("/api/auth/sso/providers")
    def list_sso_providers():
        """返回已配置的 SSO Provider 列表（前端用于渲染登录按钮）。"""
        with db() as conn:
            settings = get_platform_settings(conn)
        return [p.info() for p in load_providers(settings).values()]

    @app.get("/api/auth/sso/start/{provider_id}")
    async def start_sso(provider_id: str):
        """发起 SSO 认证：生成防 CSRF state，302 重定向至 IdP 登录页。"""
        with db() as conn:
            settings = get_platform_settings(conn)
        providers = load_providers(settings)
        if provider_id not in providers:
            raise HTTPException(status_code=404, detail="SSO Provider 不存在")
        if not settings["sso_callback_base_url"]:
            raise HTTPException(status_code=503, detail="SSO 回调基础地址未配置")

        provider = providers[provider_id]
        state = secrets.token_urlsafe(32)
        ts = now_ts()

        with db() as conn:
            conn.execute(
                "INSERT INTO sso_states (state, provider, created_at, expires_at) VALUES (%s,%s,%s,%s)",
                (state, provider_id, ts, ts + 600),  # 10 分钟内必须完成认证
            )

        redirect_url = await provider.build_redirect_url(_callback_url(settings), state)
        return RedirectResponse(url=redirect_url, status_code=302)

    @app.post("/api/auth/sso/callback")
    async def sso_callback(payload: SSOCallbackInput):
        """处理 IdP 回调：验证 state、换取身份、查找/创建用户、返回 session token。"""
        ts = now_ts()
        with db() as conn:
            settings = get_platform_settings(conn)
        providers = load_providers(settings)

        # 原子删除并读取 state 记录（防重放攻击）
        with db() as conn:
            state_row = conn.execute(
                "DELETE FROM sso_states WHERE state=%s AND expires_at>=%s RETURNING *",
                (payload.state, ts),
            ).fetchone()

        if not state_row:
            raise HTTPException(status_code=400, detail="无效或已过期的认证状态，请重新发起登录")

        provider_id: str = state_row["provider"]
        if provider_id not in providers:
            raise HTTPException(status_code=400, detail="对应的 SSO Provider 已不存在")
        provider = providers[provider_id]

        # 向 IdP 换取外部身份信息
        try:
            identity = await provider.exchange_callback(
                {"code": payload.code, "ticket": payload.ticket, "state": payload.state},
                _callback_url(settings),
            )
        except (ValueError, httpx.HTTPStatusError, httpx.RequestError) as exc:
            raise HTTPException(status_code=400, detail=f"SSO 认证失败：{exc}") from exc

        with db() as conn:
            changed_pending_user = False

            # 1. 通过 user_identities 查找已绑定账号。禁用账号也要查出，
            #    这样首次登录创建的待审用户能稳定等待管理员审核。
            user = conn.execute(
                """SELECT u.* FROM user_identities ui
                   JOIN users u ON u.id = ui.user_id
                   WHERE ui.provider=%s AND ui.subject=%s""",
                (provider_id, identity.subject),
            ).fetchone()

            # 2. 按邮箱匹配已有账号并自动绑定
            if not user and identity.email:
                user = conn.execute(
                    "SELECT * FROM users WHERE email=%s",
                    (identity.email,),
                ).fetchone()
                if user:
                    conn.execute(
                        "INSERT INTO user_identities (user_id, provider, subject, created_at) VALUES (%s,%s,%s,%s)"
                        " ON CONFLICT (provider, subject) DO NOTHING",
                        (user["id"], provider_id, identity.subject, ts),
                    )
                    changed_pending_user = True

            # 3. 自动创建新账号
            if not user:
                if not settings["sso_auto_create_users"]:
                    raise HTTPException(
                        status_code=403,
                        detail="账号不存在，请联系管理员手动创建账号后再使用统一认证登录",
                    )
                user = _create_sso_user(conn, identity, provider_id, ts, settings, ensure_user_zfs_dataset_task)
                changed_pending_user = True

            if not user["enabled"]:
                if changed_pending_user:
                    conn.commit()
                raise HTTPException(
                    status_code=403,
                    detail="账号待审核，请等待管理员在平台用户管理中启用您的账号",
                )

            token, expires_at = create_session(conn, user["id"], ts)

        return {"token": token, "expires_at": expires_at, "user": _public_user(user)}
