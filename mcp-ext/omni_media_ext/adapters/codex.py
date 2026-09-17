"""OpenAI Codex Host Adapter with MCP and Open Agent Skills Support."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .base import BaseHostAdapter, build_stdio_entry


class CodexAdapter(BaseHostAdapter):
    """Adapter for OpenAI Codex CLI and Open Agent Skills ecosystem."""

    target_id = "codex"
    display_name = "OpenAI Codex"

    def __init__(
        self,
        custom_config_path: Optional[str | Path] = None,
        custom_skill_dir: Optional[str | Path] = None,
        server_config: Optional[str | Path] = None,
    ):
        # server_config 必须继续往上传：CLI 的 `--server-config` 会走这条路，
        # 漏掉就会在 apply 时炸成 TypeError。
        super().__init__(custom_config_path=custom_config_path, server_config=server_config)
        self.custom_skill_dir = Path(custom_skill_dir).resolve() if custom_skill_dir else None

    def get_config_path(self) -> Path:
        if self.custom_path:
            return self.custom_path

        cwd_codex = Path.cwd() / ".codex" / "config.json"
        if cwd_codex.exists():
            return cwd_codex

        return Path.home() / ".codex" / "config.json"

    def get_skill_target_path(self) -> Path:
        """Determines target path for omni-media-ext SKILL.md.

        默认写**用户级** `~/.agents/skills/omni-media-ext/SKILL.md`：早期实现会在当前工作目录
        存在 `.agents/skills/` 时写进那里，于是在技能仓库根执行 `omni-media-ext apply --target codex`
        会把 MCP 的技能文件塞进技能仓库（跨域串扰）。项目级安装必须显式传 `--skill-dir`。
        """
        if self.custom_skill_dir:
            return self.custom_skill_dir / "SKILL.md"

        # Default to user global ~/.agents/skills
        return Path.home() / ".agents" / "skills" / "omni-media-ext" / "SKILL.md"

    def get_bundled_skill_path(self) -> Path:
        """Returns bundled SKILL.md in omni_media_ext distribution."""
        return Path(__file__).resolve().parent.parent / "skills" / "omni-media-ext" / "SKILL.md"

    def build_entry(self) -> Dict[str, Any]:
        # 只注入 PYTHONPATH。
        # 原版 codex 适配器还会塞一个 `OMNI_MEDIA_OUTPUT_MODE=file`（那是为原生听音版的
        # 切片回传通道准备的旋钮）；本版本没有这个参数、也不会读它，注入一个死变量只会
        # 让宿主配置多一处误导，因此移除。
        return build_stdio_entry("omni_media_ext.server", self.server_config)

    def is_registered(self) -> bool:
        data = self.read_config()
        servers = data.get("mcpServers", {})
        mcp_registered = isinstance(servers, dict) and "omni-media-ext" in servers
        return mcp_registered

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

    def apply(self) -> Tuple[bool, str]:
        bundled_skill = self.get_bundled_skill_path()
        if not bundled_skill.is_file():
            return False, f"内置技能文件缺失，拒绝报告接入成功: {bundled_skill}"

        ok, msg = super().apply()
        target_skill = self.get_skill_target_path()
        target_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_skill, target_skill)
        msg += f" 并同步技能至: {target_skill}"
        return ok, msg

    def unapply(self) -> Tuple[bool, str]:
        ok, msg = super().unapply()
        target_skill = self.get_skill_target_path()
        if target_skill.exists():
            try:
                target_skill.unlink()
                if not any(target_skill.parent.iterdir()):
                    target_skill.parent.rmdir()
                msg += f" 并移除技能文件: {target_skill}"
            except Exception:
                pass
        return ok, msg
