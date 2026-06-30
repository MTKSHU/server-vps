"""OIDC / OAuth 2.0 Authorization Code Flow Provider。

支持任何兼容 OpenID Connect 标准的 IdP，如：
- Keycloak、Authentik 等自托管 IdP
- 飞书（Lark）、企业微信等应用内 OIDC
- 高校自建 OAuth2 服务

通过平台设置中的回调基础地址生成 /login/callback 回调 URL。
"""
from urllib.parse import urlencode

import httpx

from .base import ExternalIdentity, SSOProvider


class OIDCProvider(SSOProvider):
    def __init__(
        self,
        provider_id: str,
        display_name: str,
        issuer: str,
        client_id: str,
        client_secret: str,
        scopes: str = "openid email profile",
        # 允许手动覆盖端点，用于不完全兼容 OIDC Discovery 的 IdP
        authorization_endpoint: str = "",
        token_endpoint: str = "",
        userinfo_endpoint: str = "",
    ):
        super().__init__(provider_id, display_name)
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self._authorization_endpoint = authorization_endpoint
        self._token_endpoint = token_endpoint
        self._userinfo_endpoint = userinfo_endpoint
        self._discovery_loaded = False

    async def _ensure_endpoints(self) -> None:
        """通过 OIDC Discovery 加载端点（仅在未手动配置时执行）。"""
        if self._discovery_loaded:
            return
        if self._authorization_endpoint and self._token_endpoint and self._userinfo_endpoint:
            self._discovery_loaded = True
            return
        discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            config = resp.json()
        if not self._authorization_endpoint:
            self._authorization_endpoint = config["authorization_endpoint"]
        if not self._token_endpoint:
            self._token_endpoint = config["token_endpoint"]
        if not self._userinfo_endpoint:
            self._userinfo_endpoint = config.get("userinfo_endpoint", "")
        self._discovery_loaded = True

    async def build_redirect_url(self, callback_url: str, state: str) -> str:
        await self._ensure_endpoints()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": callback_url,
            "scope": self.scopes,
            "state": state,
        }
        return f"{self._authorization_endpoint}?{urlencode(params)}"

    async def exchange_callback(self, params: dict[str, str], callback_url: str) -> ExternalIdentity:
        code = params.get("code", "").strip()
        if not code:
            raise ValueError("OIDC 回调中缺少 code 参数")
        await self._ensure_endpoints()
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. 用 code 换取 access_token
            token_resp = await client.post(
                self._token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback_url,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()
            access_token = tokens.get("access_token", "")
            if not access_token:
                raise ValueError("IdP 未返回 access_token")
            # 2. 用 access_token 获取用户信息
            if not self._userinfo_endpoint:
                raise ValueError("OIDC userinfo 端点未配置")
            userinfo_resp = await client.get(
                self._userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_resp.raise_for_status()
            userinfo = userinfo_resp.json()

        subject = userinfo.get("sub", "")
        if not subject:
            raise ValueError("IdP 未返回 sub（用户唯一标识）")
        email = userinfo.get("email", "")
        display_name = (
            userinfo.get("name", "")
            or userinfo.get("display_name", "")
            or userinfo.get("preferred_username", "")
        )
        # 从 preferred_username 或邮箱前缀提取用户名建议
        username_hint = (
            userinfo.get("preferred_username", "").split("@")[0]
            or email.split("@")[0]
        )
        # 学/工号字段（不同 IdP 属性名不同）
        staff_id = (
            userinfo.get("employee_number", "")
            or userinfo.get("student_number", "")
            or userinfo.get("staff_id", "")
            or userinfo.get("uid", "")
        )
        return ExternalIdentity(
            subject=subject,
            email=email,
            display_name=display_name,
            username_hint=username_hint,
            staff_id=staff_id,
            extra=userinfo,
        )
