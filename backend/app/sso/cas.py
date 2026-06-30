"""CAS 2.0 / 3.0 Provider。

中国高校教育网统一认证（CAS）的常见协议实现：
- CAS 2.0: /serviceValidate 端点，XML 响应，仅返回 username
- CAS 3.0: /p3/serviceValidate 端点，XML 响应，可返回更多属性

state 参数通过附加到 service URL 的方式传递，
CAS 回调时会把 ticket 附加到原始 service URL。
"""
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, quote

import httpx

from .base import ExternalIdentity, SSOProvider

_CAS_NS = "http://www.yale.edu/tp/cas"


def _cas_service_url(callback_url: str, state: str) -> str:
    """构造带 state 的 CAS service URL（用于防 CSRF）。"""
    sep = "&" if "?" in callback_url else "?"
    return f"{callback_url}{sep}state={quote(state, safe='')}"


class CASProvider(SSOProvider):
    def __init__(
        self,
        provider_id: str,
        display_name: str,
        server_url: str,
        version: int = 3,
    ):
        super().__init__(provider_id, display_name)
        self.server_url = server_url.rstrip("/")
        self.version = version  # 2 或 3

    async def build_redirect_url(self, callback_url: str, state: str) -> str:
        service = _cas_service_url(callback_url, state)
        return f"{self.server_url}/login?{urlencode({'service': service})}"

    async def exchange_callback(self, params: dict[str, str], callback_url: str) -> ExternalIdentity:
        ticket = params.get("ticket", "").strip()
        state = params.get("state", "").strip()
        if not ticket:
            raise ValueError("CAS 回调中缺少 ticket 参数")
        # 重建 service URL（必须与登录时完全一致）
        service = _cas_service_url(callback_url, state)
        # 选择验证端点
        validate_path = "/p3/serviceValidate" if self.version >= 3 else "/serviceValidate"
        validate_url = f"{self.server_url}{validate_path}?{urlencode({'service': service, 'ticket': ticket})}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(validate_url)
            resp.raise_for_status()
        return self._parse_xml(resp.text)

    def _parse_xml(self, xml_text: str) -> ExternalIdentity:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise ValueError(f"CAS 响应解析失败: {e}") from e
        ns = _CAS_NS
        success = root.find(f"{{{ns}}}authenticationSuccess")
        if success is None:
            failure = root.find(f"{{{ns}}}authenticationFailure")
            msg = (failure.text or "").strip() if failure is not None else "未知错误"
            raise ValueError(f"CAS 认证失败: {msg}")
        user_elem = success.find(f"{{{ns}}}user")
        username = (user_elem.text or "").strip() if user_elem is not None else ""
        if not username:
            raise ValueError("CAS 响应中缺少用户名")

        def _attr(tag: str) -> str:
            attrs = success.find(f"{{{ns}}}attributes")
            if attrs is None:
                return ""
            el = attrs.find(f"{{{ns}}}{tag}")
            return (el.text or "").strip() if el is not None else ""

        email = _attr("mail") or _attr("email") or _attr("emailAddress")
        display_name = _attr("cn") or _attr("displayName") or _attr("name") or username
        staff_id = (
            _attr("employeeNumber")
            or _attr("studentNumber")
            or _attr("staffId")
            or _attr("uid")
            or username
        )
        return ExternalIdentity(
            subject=username,
            email=email,
            display_name=display_name,
            username_hint=username,
            staff_id=staff_id,
        )
