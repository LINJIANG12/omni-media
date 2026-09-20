"""Host adapters for MCP integration."""

from .base import BaseHostAdapter, build_generic_config, build_stdio_entry
from .registry import get_adapter, get_all_adapters, list_supported_targets

__all__ = [
    "BaseHostAdapter",
    "build_generic_config",
    "build_stdio_entry",
    "get_adapter",
    "get_all_adapters",
    "list_supported_targets",
]
