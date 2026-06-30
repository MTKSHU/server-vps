from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ExternalIdentity:
    """IdP 返回的标准化外部身份信息。"""
    subject: str            # IdP 内的唯一标识（OIDC sub / CAS username）
    email: str = ""         # 邮箱（可选）
    display_name: str = ""  # 显示名（可选）
    username_hint: str = "" # 建议的系统用户名（如学号、邮箱前缀）
    staff_id: str = ""      # 学/工号（可选）
    extra: dict = field(default_factory=dict)  # IdP 原始属性


class SSOProvider(ABC):
    """统一认证 Provider 抽象基类。

    每种协议（OIDC、CAS 等）实现此接口即可接入统一认证入口。
    """

    def __init__(self, provider_id: str, display_name: str):
        self.provider_id = provider_id    # 格式: "type:name"，如 "cas:seu"
        self.display_name = display_name  # 显示给用户的名称

    @abstractmethod
    async def build_redirect_url(self, callback_url: str, state: str) -> str:
        """构造跳转到 IdP 的认证 URL。

        Args:
            callback_url: 认证完成后回调的前端地址
            state: 防 CSRF 随机态（已存入 DB）
        """
        ...

    @abstractmethod
    async def exchange_callback(self, params: dict[str, str], callback_url: str) -> ExternalIdentity:
        """用 IdP 回调参数换取外部身份信息。

        Args:
            params: 回调中的 URL 参数，如 {"code": ..., "state": ...} 或 {"ticket": ..., "state": ...}
            callback_url: 与 build_redirect_url 中使用的相同回调地址
        """
        ...

    def info(self) -> dict[str, str]:
        """返回展示给前端的 Provider 信息。"""
        return {"id": self.provider_id, "display_name": self.display_name}
