from typing import Any


def password_change_enabled(settings: dict[str, Any], sso_enabled: bool) -> bool:
    """Local password changes are irrelevant while OIDC owns authentication."""
    return not (sso_enabled and settings.get("sso_provider_type") == "oidc")
