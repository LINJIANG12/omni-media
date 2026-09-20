"""协议注册表：`protocol` 字符串 → 端点实现类。

新增协议时只需在这里登记，并在 `core/limits.py` 的 `PROTOCOL_WHITELIST` 里放行
（配置校验与注册表两处必须同时改，否则要么配不出来、要么配出来没人实现）。
"""

from __future__ import annotations

from typing import Dict, Type

from ..config import Config, Defaults, Endpoint
from .base import BaseEndpoint
from .gemini import GeminiEndpoint
from .openai import OpenAIEndpoint

ENDPOINT_MAP: Dict[str, Type[BaseEndpoint]] = {
    "gemini": GeminiEndpoint,
    "openai": OpenAIEndpoint,
}


def build_endpoint(endpoint: Endpoint, defaults: Defaults) -> BaseEndpoint:
    """按 protocol 实例化端点；未登记的协议给出明确错误。"""
    cls = ENDPOINT_MAP.get(endpoint.protocol)
    if cls is None:
        raise ValueError(
            f"未实现的协议: `{endpoint.protocol}`（端点 {endpoint.name}）。"
            f"已实现: {sorted(ENDPOINT_MAP)}"
        )
    return cls(endpoint=endpoint, defaults=defaults)


def build_endpoint_from_config(config: Config, name: str | None = None) -> BaseEndpoint:
    """从配置里解析端点名并实例化。"""
    return build_endpoint(config.resolve(name), config.defaults)


def list_protocols() -> list[str]:
    return sorted(ENDPOINT_MAP)
