"""SSO Provider 注册表，由平台设置动态构建 Provider。"""
from .base import SSOProvider
from .cas import CASProvider
from .oidc import OIDCProvider


def _provider_id(settings: dict) -> str:
    return f"{settings['sso_provider_type']}:{settings['sso_provider_name']}"


def _build_provider(settings: dict) -> SSOProvider | None:
    """根据平台设置构建 Provider 实例。"""
    if not settings.get("sso_provider_enabled"):
        return None
    ptype = settings["sso_provider_type"]
    provider_id = _provider_id(settings)
    display_name = settings["sso_provider_display_name"] or provider_id

    if ptype == "cas":
        server_url = settings["sso_cas_server_url"]
        if not server_url:
            return None
        version = int(settings["sso_cas_version"] or 3)
        return CASProvider(provider_id, display_name, server_url, version)

    if ptype == "oidc":
        issuer = settings["sso_oidc_issuer"]
        client_id = settings["sso_oidc_client_id"]
        client_secret = settings["sso_oidc_client_secret"]
        auth_ep = settings["sso_oidc_authorization_endpoint"]
        token_ep = settings["sso_oidc_token_endpoint"]
        userinfo_ep = settings["sso_oidc_userinfo_endpoint"]
        # client_id 和 client_secret 必填；issuer 仅在未完整指定端点时必填（用于 OIDC Discovery）
        if not (client_id and client_secret):
            return None
        if not issuer and not (auth_ep and token_ep and userinfo_ep):
            return None
        return OIDCProvider(
            provider_id,
            display_name,
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            scopes=settings["sso_oidc_scopes"],
            authorization_endpoint=auth_ep,
            token_endpoint=token_ep,
            userinfo_endpoint=userinfo_ep,
        )

    return None


def load_providers(settings: dict) -> dict[str, SSOProvider]:
    """从平台设置加载并返回有效 Provider。"""
    provider = _build_provider(settings)
    return {_provider_id(settings): provider} if provider else {}
