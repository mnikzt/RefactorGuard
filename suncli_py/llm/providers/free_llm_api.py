"""Free LLM API 提供商。

对应 ``com.paicli.llm.FreeLlmApiClient``。
用于本地/自建代理服务。
"""

from __future__ import annotations

from suncli_py.llm.base import AbstractOpenAiCompatibleClient

DEFAULT_MODEL = "gpt-4o-mini"


class FreeLlmApiClient(AbstractOpenAiCompatibleClient):
    """Free LLM API 客户端（本地代理）。"""

    def __init__(self, api_key: str, model: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._api_url = base_url or "http://localhost:8000/v1/chat/completions"

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_model(self) -> str:
        return self._model

    def _get_api_key(self) -> str:
        return self._api_key

    def _customize_body(self, body: dict) -> None:
        body["stream_options"] = {"include_usage": True}

    @property
    def provider_name(self) -> str:
        return "freellmapi"
