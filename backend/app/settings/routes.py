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
        if payload.shared_storage_mode != "disabled":
            if not payload.nfs_server.strip():
                raise HTTPException(status_code=400, detail="启用共享存储时必须配置 NFS 服务地址")
        if payload.truenas_nfs_auto_share and not payload.nfs_users_export.strip():
            raise HTTPException(status_code=400, detail="自动创建 TrueNAS NFS Share 时必须配置父 Share 模板路径")
        if any(option.strip().lower().startswith("soft") or option.strip().lower() == "soft" for option in payload.nfs_mount_options.split(",")):
            raise HTTPException(status_code=400, detail="NFS 挂载禁止使用 soft 选项")
        nfs_options = {option.strip().lower() for option in payload.nfs_mount_options.split(",") if option.strip()}
        if ("vers=4.1" not in nfs_options and "nfsvers=4.1" not in nfs_options) or "proto=tcp" not in nfs_options or "hard" not in nfs_options:
            raise HTTPException(status_code=400, detail="NFS 必须使用 hard、vers=4.1 和 proto=tcp")
        if not re.fullmatch(r"\.?[A-Za-z0-9][A-Za-z0-9._-]{1,80}", payload.nfs_sentinel.strip()):
            raise HTTPException(status_code=400, detail="NFS sentinel 文件名不合法")
        if payload.shared_storage_mode != "disabled" and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", payload.nfs_sentinel_signature.strip()):
            raise HTTPException(status_code=400, detail="启用共享存储时 sentinel 签名必须为 16-128 位安全字符")
        if payload.nfs_idmap_base < 65536 or payload.nfs_idmap_base > 2_000_000_000:
            raise HTTPException(status_code=400, detail="NFS idmap base 不合法")
        if not 1 <= payload.workspace_default_gb <= 100000 or not 1 <= payload.workspace_retention_days <= 3650:
            raise HTTPException(status_code=400, detail="workspace 默认容量或保留天数不合法")
        interval_limits = {
            "实时指标": (payload.agent_metrics_interval_seconds, 1, 60),
            "节点心跳": (payload.agent_heartbeat_interval_seconds, 5, 300),
            "容器状态": (payload.agent_container_interval_seconds, 5, 300),
            "存储容量": (payload.agent_storage_interval_seconds, 30, 3600),
            "静态清单": (payload.agent_inventory_interval_seconds, 60, 86400),
            "任务轮询": (payload.agent_task_poll_interval_seconds, 1, 60),
        }
        for label, (value, minimum, maximum) in interval_limits.items():
            if value < minimum or value > maximum:
                raise HTTPException(status_code=400, detail=f"{label}周期必须在 {minimum}-{maximum} 秒之间")
        if payload.agent_container_interval_seconds < payload.agent_heartbeat_interval_seconds:
            raise HTTPException(status_code=400, detail="容器状态周期不能小于节点心跳周期")
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
