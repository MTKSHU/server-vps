from typing import Any

from ..auth import is_admin_user


def image_available_to_user(image: dict[str, Any], user: dict[str, Any] | None) -> bool:
    """System-owned images are reserved for internal workflows."""
    return image.get("owner") != "system" or is_admin_user(user)
