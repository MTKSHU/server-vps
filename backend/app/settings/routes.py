from typing import Any
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from ..auth import require_admin
from ..platform_settings import get_platform_settings, platform_settings_to_rows
from ..schemas import PlatformSettingsInput


def register_settings_routes(app, deps: dict[str, Any]):
    db = deps["db"]
    now_ts = deps["now_ts"]
    audit = deps["audit"]

    @app.get("/api/platform/settings")
    def platform_settings_get():
        require_admin()
        with db() as conn:
            return get_platform_settings(conn)

    @app.put("/api/platform/settings")
    def platform_settings_put(payload: PlatformSettingsInput):
        actor = require_admin()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", payload.sso_provider_name):
            raise HTTPException(status_code=400, detail="SSO Provider 标识只能包含小写字母、数字、下划线和中划线")
        try:
            ZoneInfo(payload.platform_timezone.strip() or "Asia/Shanghai")
        except ZoneInfoNotFoundError as exc:
            raise HTTPException(status_code=400, detail="平台时区不合法，请使用 IANA 时区名，例如 Asia/Shanghai") from exc
        if payload.transfer_bandwidth_limit_mbps < 0 or payload.transfer_bandwidth_limit_mbps > 100000:
            raise HTTPException(status_code=400, detail="传输带宽上限必须在 0-100000 Mbps 之间")
        if payload.sso_provider_enabled and not payload.sso_callback_base_url.strip():
            raise HTTPException(status_code=400, detail="启用 SSO 时必须填写回调基础地址")
        if payload.sso_provider_enabled and payload.sso_provider_type == "cas" and not payload.sso_cas_server_url.strip():
            raise HTTPException(status_code=400, detail="启用 CAS 时必须填写 CAS 服务地址")
        if payload.sso_provider_enabled and payload.sso_provider_type == "oidc":
            has_endpoints = (
                payload.sso_oidc_authorization_endpoint.strip()
                and payload.sso_oidc_token_endpoint.strip()
                and payload.sso_oidc_userinfo_endpoint.strip()
            )
            if not payload.sso_oidc_client_id.strip() or not payload.sso_oidc_client_secret.strip():
                raise HTTPException(status_code=400, detail="启用 OIDC 时必须填写 Client ID 和 Client Secret")
            if not payload.sso_oidc_issuer.strip() and not has_endpoints:
                raise HTTPException(status_code=400, detail="启用 OIDC 时必须填写 Issuer，或完整填写授权/令牌/用户信息端点")
        values = platform_settings_to_rows(payload.model_dump())
        ts = now_ts()
        with db() as conn:
            for key, value in values.items():
                conn.execute(
                    """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (key, value, ts),
                )
            audit(conn, actor["username"], "update", "system-settings:platform", payload.model_dump())
            return get_platform_settings(conn)
