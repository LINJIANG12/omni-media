"""Google Antigravity Host Adapter for MCP Configuration."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional

from .base import BaseHostAdapter, build_stdio_entry


class AntigravityAdapter(BaseHostAdapter):
    """Adapter for Google Antigravity Agent runtime."""

    target_id = "antigravity"
    display_name = "Google Antigravity"

    def get_config_path(self) -> Path:
        if self.custom_path:
            return self.custom_path

        return Path.home() / ".gemini" / "config" / "mcp_config.json"

    def build_entry(self) -> Dict[str, Any]:
        return build_stdio_entry("omni_media_ext.server", self.server_config)

    def is_registered(self) -> bool:
        data = self.read_config()
        servers = data.get("mcpServers", {})
        return isinstance(servers, dict) and "omni-media-ext" in servers

    def build_applied_config(self, current_data: Dict[str, Any]) -> Dict[str, Any]:
        data = copy.deepcopy(current_data)
        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}
        data["mcpServers"]["omni-media-ext"] = self.build_entry()
        return data

    def build_unapplied_config(self, current_data: Dict[str, Any]) -> Dict[str, Any]:
        data = copy.deepcopy(current_data)
        if "mcpServers" in data and isinstance(data["mcpServers"], dict) and "omni-media-ext" in data["mcpServers"]:
            del data["mcpServers"]["omni-media-ext"]
        return data
