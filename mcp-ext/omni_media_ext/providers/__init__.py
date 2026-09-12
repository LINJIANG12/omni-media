"""外部模型端点实现（两种协议：gemini / openai）。"""

from __future__ import annotations

from .base import BaseEndpoint, ProcessingResult, ProviderRequestError
from .gemini import GeminiEndpoint
from .openai import OpenAIEndpoint
from .registry import build_endpoint, build_endpoint_from_config, list_protocols

__all__ = [
    "BaseEndpoint",
    "ProcessingResult",
    "ProviderRequestError",
    "GeminiEndpoint",
    "OpenAIEndpoint",
    "build_endpoint",
    "build_endpoint_from_config",
    "list_protocols",
]
