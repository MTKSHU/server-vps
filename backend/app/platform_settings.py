import os
from typing import Any


GROUP_NAMES = ("platform_admin", "admin", "member", "guest")
SSO_PROVIDER_TYPES = ("oidc", "cas")


def bool_to_setting(value: bool) -> str:
    return "1" if value else "0"


def setting_to_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def effective_node_shared_storage_mode(
    node: dict[str, Any], settings: dict[str, Any], user_id: int | None = None
) -> str:
    """Resolve the policy for new mounts; existing container mounts stay unchanged."""
    override = str(node.get("shared_storage_mode") or "inherit").strip().lower()
    if override in ("disabled", "enabled"):
        return override
    global_mode = settings.get("shared_storage_mode", "disabled")
    if global_mode == "enabled":
        return "enabled"
    if global_mode == "canary":
        canary_ids = settings.get("shared_storage_canary_user_ids") or []
        if user_id is None:
            return "enabled" if canary_ids else "disabled"
        return "enabled" if user_id in canary_ids else "disabled"
    return "disabled"


PLATFORM_SETTING_DEFAULTS = {
    "local_login_enabled": "1",
    "platform_registration_enabled": "0",
    "platform_registration_auto_enable": "0",
    "platform_registration_default_group": "member",
    "sso_registration_enabled": "1",
    "sso_auto_create_users": "1",
    "sso_auto_enable_new_users": "0",
    "sso_default_group": "member",
    "platform_timezone": "Asia/Shanghai",
    "transfer_bandwidth_limit_mbps": "0",
    "shared_storage_mode": "disabled",
    "shared_storage_canary_user_ids": "",
    "nfs_server": "",
    "nfs_users_export": "",
    "nfs_datasets_export": "",
    "nfs_models_export": "",
    "nfs_mount_options": "hard,_netdev,noatime,vers=4.1,proto=tcp",
    "nfs_sentinel": ".server-vps-nfs",
    "nfs_sentinel_signature": "",
    "nfs_idmap_base": "1000000",
    "truenas_nfs_auto_share": "0",
    "workspace_default_gb": "100",
    "workspace_retention_days": "30",
    "agent_metrics_interval_seconds": "2",
    "agent_heartbeat_interval_seconds": "15",
    "agent_container_interval_seconds": "15",
    "agent_storage_interval_seconds": "60",
    "agent_inventory_interval_seconds": "300",
    "agent_task_poll_interval_seconds": "5",
    "webhook_enabled": "0",
    "webhook_url": "",
    "webhook_secret": "",
    "sso_provider_enabled": "0",
    "sso_provider_type": "oidc",
    "sso_provider_name": "casdoor",
    "sso_provider_display_name": "统一认证",
    "sso_callback_base_url": "",
    "sso_cas_server_url": "",
    "sso_cas_version": "3",
    "sso_oidc_issuer": "",
    "sso_oidc_authorization_endpoint": "",
    "sso_oidc_token_endpoint": "",
    "sso_oidc_userinfo_endpoint": "",
    "sso_oidc_client_id": "",
    "sso_oidc_client_secret": "",
    "sso_oidc_scopes": "openid profile email",
    "sso_casdoor_admin_owner": "built-in",
}


def _legacy_sso_defaults() -> dict[str, str]:
    raw_providers = os.environ.get("SSO_PROVIDERS", "").strip()
    provider_id = next((item.strip() for item in raw_providers.split(",") if item.strip()), "")
    if ":" in provider_id:
        provider_type, provider_name = provider_id.split(":", 1)
    else:
        provider_type, provider_name = "oidc", "casdoor"
    provider_type = provider_type if provider_type in SSO_PROVIDER_TYPES else "oidc"
    provider_name = provider_name or "casdoor"
    prefix = f"SSO_{provider_type.upper()}_{provider_name.upper()}_"
    values = {
        "sso_provider_enabled": "1" if provider_id else "0",
        "sso_provider_type": provider_type,
        "sso_provider_name": provider_name,
        "sso_provider_display_name": os.environ.get(f"{prefix}DISPLAY_NAME", "统一认证"),
        "sso_callback_base_url": os.environ.get("SSO_CALLBACK_BASE_URL", ""),
        "sso_casdoor_admin_owner": os.environ.get("CASDOOR_ADMIN_OWNER", "built-in"),
    }
    if provider_type == "cas":
        values.update(
            {
                "sso_cas_server_url": os.environ.get(f"{prefix}SERVER_URL", ""),
                "sso_cas_version": os.environ.get(f"{prefix}VERSION", "3"),
            }
        )
    else:
        values.update(
            {
                "sso_oidc_issuer": os.environ.get(f"{prefix}ISSUER", ""),
                "sso_oidc_authorization_endpoint": os.environ.get(f"{prefix}AUTHORIZATION_ENDPOINT", ""),
                "sso_oidc_token_endpoint": os.environ.get(f"{prefix}TOKEN_ENDPOINT", ""),
                "sso_oidc_userinfo_endpoint": os.environ.get(f"{prefix}USERINFO_ENDPOINT", ""),
                "sso_oidc_client_id": os.environ.get(f"{prefix}CLIENT_ID", ""),
                "sso_oidc_client_secret": os.environ.get(f"{prefix}CLIENT_SECRET", ""),
                "sso_oidc_scopes": os.environ.get(f"{prefix}SCOPES", "openid profile email"),
            }
        )
    return values


def get_platform_settings(conn) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT key, value FROM system_settings WHERE key = ANY(%s)",
        (list(PLATFORM_SETTING_DEFAULTS.keys()),),
    ).fetchall()
    raw = {**PLATFORM_SETTING_DEFAULTS, **_legacy_sso_defaults()}
    for row in rows:
        raw[row["key"]] = row["value"]
    platform_group = raw["platform_registration_default_group"]
    sso_group = raw["sso_default_group"]
    canary_ids = []
    for value in str(raw["shared_storage_canary_user_ids"] or "").split(","):
        try:
            parsed = int(value.strip())
            if parsed > 0 and parsed not in canary_ids:
                canary_ids.append(parsed)
        except ValueError:
            continue
    shared_mode = raw["shared_storage_mode"] if raw["shared_storage_mode"] in ("disabled", "canary", "enabled") else "disabled"
    return {
        "local_login_enabled": setting_to_bool(raw["local_login_enabled"]),
        "platform_registration_enabled": setting_to_bool(raw["platform_registration_enabled"]),
        "platform_registration_auto_enable": setting_to_bool(raw["platform_registration_auto_enable"]),
        "platform_registration_default_group": platform_group if platform_group in GROUP_NAMES else "member",
        "sso_registration_enabled": setting_to_bool(raw["sso_registration_enabled"]),
        "sso_auto_create_users": setting_to_bool(raw["sso_auto_create_users"]),
        "sso_auto_enable_new_users": setting_to_bool(raw["sso_auto_enable_new_users"]),
        "sso_default_group": sso_group if sso_group in GROUP_NAMES else "member",
        "platform_timezone": raw["platform_timezone"] or "Asia/Shanghai",
        "transfer_bandwidth_limit_mbps": max(0, int(raw["transfer_bandwidth_limit_mbps"] or "0")),
        "shared_storage_mode": shared_mode,
        "shared_storage_canary_user_ids": canary_ids,
        "nfs_server": raw["nfs_server"].strip(),
        "nfs_users_export": raw["nfs_users_export"].strip(),
        "nfs_datasets_export": raw["nfs_datasets_export"].strip(),
        "nfs_models_export": raw["nfs_models_export"].strip(),
        "nfs_mount_options": raw["nfs_mount_options"].strip(),
        "nfs_sentinel": raw["nfs_sentinel"].strip() or ".server-vps-nfs",
        "nfs_sentinel_signature": raw["nfs_sentinel_signature"].strip(),
        "nfs_idmap_base": max(65536, int(raw["nfs_idmap_base"] or "1000000")),
        "truenas_nfs_auto_share": setting_to_bool(raw["truenas_nfs_auto_share"]),
        "workspace_default_gb": max(1, int(raw["workspace_default_gb"] or "100")),
        "workspace_retention_days": max(1, int(raw["workspace_retention_days"] or "30")),
        "agent_metrics_interval_seconds": max(1, int(raw["agent_metrics_interval_seconds"] or "2")),
        "agent_heartbeat_interval_seconds": max(5, int(raw["agent_heartbeat_interval_seconds"] or "15")),
        "agent_container_interval_seconds": max(5, int(raw["agent_container_interval_seconds"] or "15")),
        "agent_storage_interval_seconds": max(30, int(raw["agent_storage_interval_seconds"] or "60")),
        "agent_inventory_interval_seconds": max(60, int(raw["agent_inventory_interval_seconds"] or "300")),
        "agent_task_poll_interval_seconds": max(1, int(raw["agent_task_poll_interval_seconds"] or "5")),
        "webhook_enabled": setting_to_bool(raw["webhook_enabled"]),
        "webhook_url": raw["webhook_url"],
        "webhook_secret": raw["webhook_secret"],
        "sso_provider_enabled": setting_to_bool(raw["sso_provider_enabled"]),
        "sso_provider_type": raw["sso_provider_type"] if raw["sso_provider_type"] in SSO_PROVIDER_TYPES else "oidc",
        "sso_provider_name": raw["sso_provider_name"] or "casdoor",
        "sso_provider_display_name": raw["sso_provider_display_name"],
        "sso_callback_base_url": raw["sso_callback_base_url"],
        "sso_cas_server_url": raw["sso_cas_server_url"],
        "sso_cas_version": int(raw["sso_cas_version"] or "3"),
        "sso_oidc_issuer": raw["sso_oidc_issuer"],
        "sso_oidc_authorization_endpoint": raw["sso_oidc_authorization_endpoint"],
        "sso_oidc_token_endpoint": raw["sso_oidc_token_endpoint"],
        "sso_oidc_userinfo_endpoint": raw["sso_oidc_userinfo_endpoint"],
        "sso_oidc_client_id": raw["sso_oidc_client_id"],
        "sso_oidc_client_secret": raw["sso_oidc_client_secret"],
        "sso_oidc_scopes": raw["sso_oidc_scopes"] or "openid profile email",
        "sso_casdoor_admin_owner": raw["sso_casdoor_admin_owner"] or "built-in",
    }


def get_agent_collection_config(conn) -> dict[str, int]:
    settings = get_platform_settings(conn)
    return {
        "metrics_interval_seconds": settings["agent_metrics_interval_seconds"],
        "heartbeat_interval_seconds": settings["agent_heartbeat_interval_seconds"],
        "container_interval_seconds": settings["agent_container_interval_seconds"],
        "storage_interval_seconds": settings["agent_storage_interval_seconds"],
        "inventory_interval_seconds": settings["agent_inventory_interval_seconds"],
        "task_poll_interval_seconds": settings["agent_task_poll_interval_seconds"],
    }


def platform_settings_to_rows(settings: dict[str, Any]) -> dict[str, str]:
    return {
        "local_login_enabled": bool_to_setting(bool(settings["local_login_enabled"])),
        "platform_registration_enabled": bool_to_setting(bool(settings["platform_registration_enabled"])),
        "platform_registration_auto_enable": bool_to_setting(bool(settings["platform_registration_auto_enable"])),
        "platform_registration_default_group": (
            settings["platform_registration_default_group"]
            if settings["platform_registration_default_group"] in GROUP_NAMES
            else "member"
        ),
        "sso_registration_enabled": bool_to_setting(bool(settings["sso_registration_enabled"])),
        "sso_auto_create_users": bool_to_setting(bool(settings["sso_auto_create_users"])),
        "sso_auto_enable_new_users": bool_to_setting(bool(settings["sso_auto_enable_new_users"])),
        "sso_default_group": settings["sso_default_group"] if settings["sso_default_group"] in GROUP_NAMES else "member",
        "platform_timezone": settings["platform_timezone"].strip() or "Asia/Shanghai",
        "transfer_bandwidth_limit_mbps": str(max(0, int(settings["transfer_bandwidth_limit_mbps"] or 0))),
        "shared_storage_mode": settings["shared_storage_mode"] if settings["shared_storage_mode"] in ("disabled", "canary", "enabled") else "disabled",
        "shared_storage_canary_user_ids": ",".join(str(int(item)) for item in settings["shared_storage_canary_user_ids"] if int(item) > 0),
        "nfs_server": settings["nfs_server"].strip(),
        "nfs_users_export": settings["nfs_users_export"].strip(),
        "nfs_datasets_export": settings["nfs_datasets_export"].strip(),
        "nfs_models_export": settings["nfs_models_export"].strip(),
        "nfs_mount_options": settings["nfs_mount_options"].strip(),
        "nfs_sentinel": settings["nfs_sentinel"].strip() or ".server-vps-nfs",
        "nfs_sentinel_signature": settings["nfs_sentinel_signature"].strip(),
        "nfs_idmap_base": str(max(65536, int(settings["nfs_idmap_base"]))),
        "truenas_nfs_auto_share": bool_to_setting(bool(settings["truenas_nfs_auto_share"])),
        "workspace_default_gb": str(max(1, int(settings["workspace_default_gb"]))),
        "workspace_retention_days": str(max(1, int(settings["workspace_retention_days"]))),
        "agent_metrics_interval_seconds": str(int(settings["agent_metrics_interval_seconds"])),
        "agent_heartbeat_interval_seconds": str(int(settings["agent_heartbeat_interval_seconds"])),
        "agent_container_interval_seconds": str(int(settings["agent_container_interval_seconds"])),
        "agent_storage_interval_seconds": str(int(settings["agent_storage_interval_seconds"])),
        "agent_inventory_interval_seconds": str(int(settings["agent_inventory_interval_seconds"])),
        "agent_task_poll_interval_seconds": str(int(settings["agent_task_poll_interval_seconds"])),
        "webhook_enabled": bool_to_setting(bool(settings["webhook_enabled"])),
        "webhook_url": settings["webhook_url"].strip(),
        "webhook_secret": settings["webhook_secret"].strip(),
        "sso_provider_enabled": bool_to_setting(bool(settings["sso_provider_enabled"])),
        "sso_provider_type": settings["sso_provider_type"] if settings["sso_provider_type"] in SSO_PROVIDER_TYPES else "oidc",
        "sso_provider_name": settings["sso_provider_name"].strip() or "casdoor",
        "sso_provider_display_name": settings["sso_provider_display_name"].strip() or "统一认证",
        "sso_callback_base_url": settings["sso_callback_base_url"].strip().rstrip("/"),
        "sso_cas_server_url": settings["sso_cas_server_url"].strip().rstrip("/"),
        "sso_cas_version": str(settings["sso_cas_version"] or 3),
        "sso_oidc_issuer": settings["sso_oidc_issuer"].strip().rstrip("/"),
        "sso_oidc_authorization_endpoint": settings["sso_oidc_authorization_endpoint"].strip(),
        "sso_oidc_token_endpoint": settings["sso_oidc_token_endpoint"].strip(),
        "sso_oidc_userinfo_endpoint": settings["sso_oidc_userinfo_endpoint"].strip(),
        "sso_oidc_client_id": settings["sso_oidc_client_id"].strip(),
        "sso_oidc_client_secret": settings["sso_oidc_client_secret"].strip(),
        "sso_oidc_scopes": settings["sso_oidc_scopes"].strip() or "openid profile email",
        "sso_casdoor_admin_owner": settings["sso_casdoor_admin_owner"].strip() or "built-in",
    }
