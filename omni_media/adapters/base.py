"""Base Host Adapter for MCP configuration management."""

from __future__ import annotations

import copy
import difflib
import json
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def strip_json_comments(text: str) -> str:
    """Strips comments from JSONC text while preserving string literals."""
    pattern = r'("(?:\\.|[^"\\])*")|(/\*[\s\S]*?\*/|//[^\r\n]*)'

    def replace(match):
        if match.group(1):
            return match.group(1)
        return ""

    return re.sub(pattern, replace, text)


def get_default_env_vars() -> Dict[str, str]:
    """Returns the environment needed by spawned MCP server."""
    proj_root = str(Path(__file__).resolve().parent.parent.parent)
    return {"PYTHONPATH": proj_root}


def build_stdio_entry(
    server_module: str = "omni_media.server",
    args: Optional[list[str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build canonical stdio entry used by adapters and print-config."""
    env = get_default_env_vars()
    if extra_env:
        env.update(extra_env)
    cmd_args = ["-m", server_module]
    if args:
        cmd_args.extend(args)
    return {
        "command": sys.executable,
        "args": cmd_args,
        "env": env,
    }


def build_generic_config(server_name: str = "omni-media", server_module: str = "omni_media.server") -> Dict[str, Any]:
    return {
        "mcpServers": {
            server_name: build_stdio_entry(server_module),
        }
    }


class BaseHostAdapter(ABC):
    """Unified Host Adapter parameterized by service_id."""

    target_id: str = "base"
    display_name: str = "Base Host"

    def __init__(
        self,
        custom_config_path: Optional[str | Path] = None,
        server_name: str = "omni-media",
        server_module: str = "omni_media.server",
    ):
        self.custom_path = Path(custom_config_path).resolve() if custom_config_path else None
        self.server_name = server_name
        self.server_module = server_module

    @abstractmethod
    def get_config_path(self) -> Path:
        """Target configuration file path."""
        pass

    def read_config(self) -> Dict[str, Any]:
        path = self.get_config_path()
        if not path.exists():
            return {}
        try:
            raw = path.read_text(encoding="utf-8")
            cleaned = strip_json_comments(raw).strip()
            if not cleaned:
                return {}
            return json.loads(cleaned)
        except Exception:
            return {}

    def write_config(self, data: Dict[str, Any]) -> None:
        path = self.get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        path.write_text(content, encoding="utf-8")

    def build_entry(self) -> Dict[str, Any]:
        return build_stdio_entry(self.server_module)

    def is_registered(self) -> bool:
        data = self.read_config()
        servers = data.get("mcpServers", {})
        return isinstance(servers, dict) and self.server_name in servers

    def build_applied_config(self, current_data: Dict[str, Any]) -> Dict[str, Any]:
        data = copy.deepcopy(current_data)
        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}
        data["mcpServers"][self.server_name] = self.build_entry()
        return data

    def build_unapplied_config(self, current_data: Dict[str, Any]) -> Dict[str, Any]:
        data = copy.deepcopy(current_data)
        if "mcpServers" in data and isinstance(data["mcpServers"], dict) and self.server_name in data["mcpServers"]:
            del data["mcpServers"][self.server_name]
        return data

    def preview_apply(self) -> Tuple[bool, str, Dict[str, Any]]:
        current = self.read_config()
        new_data = self.build_applied_config(current)
        old_str = json.dumps(current, indent=2, ensure_ascii=False) + "\n" if current else "{\n}\n"
        new_str = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"

        if old_str == new_str:
            return False, "", new_data

        diff = "".join(
            difflib.unified_diff(
                old_str.splitlines(keepends=True),
                new_str.splitlines(keepends=True),
                fromfile=f"a/{self.get_config_path().name}",
                tofile=f"b/{self.get_config_path().name}",
            )
        )
        return True, diff, new_data

    def preview_unapply(self) -> Tuple[bool, str, Dict[str, Any]]:
        current = self.read_config()
        if not self.is_registered():
            return False, "", current
        new_data = self.build_unapplied_config(current)
        old_str = json.dumps(current, indent=2, ensure_ascii=False) + "\n"
        new_str = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
        diff = "".join(
            difflib.unified_diff(
                old_str.splitlines(keepends=True),
                new_str.splitlines(keepends=True),
                fromfile=f"a/{self.get_config_path().name}",
                tofile=f"b/{self.get_config_path().name}",
            )
        )
        return True, diff, new_data

    def apply(self) -> Tuple[bool, str]:
        has_change, diff, new_data = self.preview_apply()
        if not has_change:
            return True, "配置已是最新，无需修改。"
        self.write_config(new_data)
        return True, f"成功配置到 {self.display_name} ({self.get_config_path()})"

    def unapply(self) -> Tuple[bool, str]:
        if not self.is_registered():
            return True, f"{self.display_name} 中未检测到 {self.server_name} 配置，跳过。"
        has_change, diff, new_data = self.preview_unapply()
        self.write_config(new_data)
        return True, f"成功从 {self.display_name} 移除配置。"

    def check_status(self) -> Dict[str, Any]:
        path = self.get_config_path()
        file_exists = path.exists()
        registered = self.is_registered()
        if registered:
            status = "PASS"
            msg = f"已正确挂载 ({path})"
        elif file_exists:
            status = "INFO"
            msg = f"配置文件存在，但未挂载 {self.server_name} ({path})"
        else:
            status = "INFO"
            msg = f"未检测到宿主配置文件 ({path})"
        return {
            "target": self.target_id,
            "display_name": self.display_name,
            "path": str(path),
            "file_exists": file_exists,
            "registered": registered,
            "status": status,
            "detail": msg,
        }
