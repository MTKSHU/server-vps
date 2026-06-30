"""Casdoor 管理 API 客户端，用于拉取已注册但尚未登录平台的待审用户。"""
import logging

import httpx

logger = logging.getLogger(__name__)


def fetch_pending_casdoor_users(known_subjects: set, settings: dict) -> list[dict]:
    endpoint = settings["sso_oidc_issuer"]
    client_id = settings["sso_oidc_client_id"]
    client_secret = settings["sso_oidc_client_secret"]
    if not (endpoint and client_id and client_secret):
        return []

    url = f"{endpoint.rstrip('/')}/api/get-users"
    params = {
        "owner": settings["sso_casdoor_admin_owner"],
        "clientId": client_id,
        "clientSecret": client_secret,
    }
    try:
        resp = httpx.get(url, params=params, timeout=5.0, verify=False)
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict):
            if body.get("status") != "ok":
                logger.warning("Casdoor /api/get-users 返回错误: %s", body.get("msg"))
                return []
            data = body.get("data") or []
        elif isinstance(body, list):
            data = body
        else:
            logger.warning("Casdoor /api/get-users 返回未知格式: %s", type(body))
            return []

        result = []
        for user in data:
            casdoor_id = user.get("id") or ""
            name = user.get("name") or ""
            if not casdoor_id or name == "admin":
                continue
            if casdoor_id in known_subjects:
                continue
            result.append(
                {
                    "casdoor_id": casdoor_id,
                    "username": name,
                    "display_name": user.get("displayName") or name,
                    "email": user.get("email") or "",
                }
            )
        return result
    except Exception as exc:
        logger.warning("拉取 Casdoor 用户列表失败: %s", exc)
        return []
