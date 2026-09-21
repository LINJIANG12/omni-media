"""Registry and implementations of Host Adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Type

from .base import BaseHostAdapter


class AntigravityAdapter(BaseHostAdapter):
    target_id = "antigravity"
    display_name = "Google Antigravity"

    def get_config_path(self) -> Path:
        if self.custom_path:
            return self.custom_path
        return Path.home() / ".gemini" / "config" / "mcp_config.json"


class CodexAdapter(BaseHostAdapter):
    target_id = "codex"
    display_name = "OpenAI Codex"

    def get_config_path(self) -> Path:
        if self.custom_path:
            return self.custom_path
        return Path.home() / ".codex" / "config.json"


class DshAdapter(BaseHostAdapter):
    target_id = "dsh"
    display_name = "DeepSeek DSH"

    def get_config_path(self) -> Path:
        if self.custom_path:
            return self.custom_path
        return Path.home() / ".dsh" / "config.json"


class OpenCodeAdapter(BaseHostAdapter):
    target_id = "opencode"
    display_name = "OpenCode Interpreter"

    def get_config_path(self) -> Path:
        if self.custom_path:
            return self.custom_path
        return Path.home() / ".config" / "opencode" / "opencode.jsonc"


class ZCodeAdapter(BaseHostAdapter):
    target_id = "zcode"
    display_name = "ZCode Studio"
    servers_path = ("mcp", "servers")

    # 一次 30 分钟切片的转录（尤其 ext 通道要调外部模型）远超 ZCode 默认的 30000ms，
    # 用默认值会在转录中途被判超时——这项必须显式下发，不是可选的美化字段。
    TIMEOUT_MS = 300000

    def build_entry(self) -> Dict[str, Any]:
        # `type` 在 ZCode 里可省略（由 `command` 推断），写上只是对齐它的规范示例。
        return {**super().build_entry(), "type": "stdio", "timeoutMs": self.TIMEOUT_MS}

    def get_config_path(self) -> Path:
        if self.custom_path:
            return self.custom_path
        cli_config = Path.home() / ".zcode" / "cli" / "config.json"
        if cli_config.exists() or (Path.home() / ".zcode" / "cli").exists():
            return cli_config
        return Path.home() / ".zcode" / "config.json"


ADAPTER_CLASSES: Dict[str, Type[BaseHostAdapter]] = {
    "antigravity": AntigravityAdapter,
    "codex": CodexAdapter,
    "dsh": DshAdapter,
    "opencode": OpenCodeAdapter,
    "zcode": ZCodeAdapter,
}


def get_adapter(
    target: str,
    custom_config_path: Optional[str | Path] = None,
    server_name: str = "omni-media",
    server_module: str = "omni_media.server",
) -> BaseHostAdapter:
    key = target.lower().strip()
    cls = ADAPTER_CLASSES.get(key)
    if not cls:
        valid = ", ".join(sorted(ADAPTER_CLASSES))
        raise ValueError(f"未知宿主目标: '{target}'。支持的目标: {valid}, all")
    return cls(custom_config_path=custom_config_path, server_name=server_name, server_module=server_module)


def list_supported_targets() -> List[str]:
    return list(ADAPTER_CLASSES.keys())


def get_all_adapters(
    server_name: str = "omni-media",
    server_module: str = "omni_media.server",
) -> List[BaseHostAdapter]:
    return [
        cls(server_name=server_name, server_module=server_module)
        for cls in ADAPTER_CLASSES.values()
    ]
