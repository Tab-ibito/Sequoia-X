"""大模型 API 适配层，仅负责 HTTP 协议，不混入选股或报告业务逻辑。"""

from abc import ABC, abstractmethod
from urllib.parse import urlparse

import requests

from sequoia_x.core.config import Settings


class LlmResponseError(ValueError):
    """模型响应缺失、拒绝或截断，不能作为完整评估使用。"""


class BaseLlmClient(ABC):
    """预留统一接口；其他 API 协议只需新增适配器实现此方法。"""

    @abstractmethod
    def complete(self, messages: list[dict[str, str]]) -> str:
        """返回模型正文中的 JSON 文本；失败时抛出异常，由评估服务统一处理。"""
        ...


class OpenAICompatibleClient(BaseLlmClient):
    """使用 requests 调用兼容 Chat Completions 的服务，无需新增 SDK。

    仅传入 model、messages、输出 token 上限和可选 JSON mode，不假设服务支持
    temperature、工具调用或特定厂商的联网参数。密钥允许为空以支持本地服务。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        base_url = settings.llm_base_url.rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or not settings.llm_model:
            raise ValueError("启用模型需配置有效的 LLM_BASE_URL 和 LLM_MODEL")
        self.url = f"{base_url}/chat/completions"

    def complete(self, messages: list[dict[str, str]]) -> str:
        """只接受正常结束的非空正文，拒绝把截断 JSON 或拒绝内容用于决策。"""
        headers = {"Content-Type": "application/json"}
        key = self.settings.llm_api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            self.settings.llm_token_parameter: self.settings.llm_max_tokens,
        }
        if self.settings.llm_json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = requests.post(
            self.url,
            json=payload,
            headers=headers,
            timeout=self.settings.llm_timeout,
            allow_redirects=False,
        )
        # 错误仅报告状态码，不转发可能含凭据、输入资料或厂商调试信息的响应体。
        if response.status_code != 200:
            raise LlmResponseError(f"模型 API HTTP {response.status_code}")
        try:
            choice = response.json()["choices"][0]
            message = choice["message"]
            content = message["content"]
            if choice.get("finish_reason") != "stop" or message.get("refusal"):
                raise LlmResponseError("模型没有正常完成评估")
            if not isinstance(content, str) or not content.strip():
                raise LlmResponseError("模型正文为空")
            return content
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmResponseError("模型响应不符合兼容协议") from exc
