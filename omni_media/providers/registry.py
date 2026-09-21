"""协议注册表：`protocol` 字符串 → 端点实现类。

新增协议时只需在 `ENDPOINT_MAP` 里登记一处即可（注册表就是唯一的放行名单：
`build_endpoint` 对未登记的协议会给出明确错误，不存在第二处需要同步的名单）。
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
