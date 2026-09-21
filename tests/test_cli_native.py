"""原生通道（`omni-media` 注册名）的 CLI 治理层测试。

迁移自 `mcp/tests/test_native_mcp.py` 里的三条 CLI 用例。旧断言是
``["-m", "omni_media.server"]``——统一后启动模块只有一个
（`omni_media.server`），**区分两条通道的不再是模块名而是 `--mode`**：
注册名 `omni-media` 对应 `--mode native`（见 `omni_media.adapters.base.SERVER_MODES`）。
"""

from __future__ import annotations

import json
from pathlib import Path

from omni_media import cli

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_print_config_registers_native_name_with_native_mode(capsys):
    assert cli.main(["print-config"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data["mcpServers"]) == {"omni-media"}
    entry = data["mcpServers"]["omni-media"]
    assert entry["args"] == ["-m", "omni_media.server", "--mode", "native"]
    assert set(entry["env"]) == {"PYTHONPATH"}
    assert Path(entry["env"]["PYTHONPATH"]) == REPO_ROOT
    assert entry["command"]


def test_apply_and_unapply_use_same_generic_command(tmp_path: Path, capsys):
    host_config = tmp_path / "dsh.json"

    assert cli.main(["apply", "--target", "dsh", "--config-path", str(host_config), "--yes"]) == 0
    capsys.readouterr()
    entry = json.loads(host_config.read_text(encoding="utf-8"))["mcpServers"]["omni-media"]
    assert entry["args"] == ["-m", "omni_media.server", "--mode", "native"]
    assert set(entry["env"]) == {"PYTHONPATH"}  # 只注入 PYTHONPATH：不带凭证

    assert cli.main(["unapply", "--target", "dsh", "--config-path", str(host_config)]) == 0
    capsys.readouterr()
    assert "omni-media" not in json.loads(host_config.read_text(encoding="utf-8"))["mcpServers"]


def test_status_reports_native_registration_name(tmp_path: Path, capsys, monkeypatch):
    """`status` 必须按本入口的注册名去查宿主配置，而不是固定查另一个名字。"""
    seen: dict[str, str] = {}
    real_get_all = cli.get_all_adapters

    def spy(server_name: str = "omni-media", server_module: str = "omni_media.server"):
        seen["server_name"] = server_name
        return real_get_all(server_name=server_name, server_module=server_module)

    monkeypatch.setattr(cli, "get_all_adapters", spy)
    assert cli.main(["status", "--config", str(tmp_path / "nope.json")]) == 0
    capsys.readouterr()
    assert seen["server_name"] == "omni-media"

    seen.clear()
    assert cli.main_ext(["status", "--config", str(tmp_path / "nope.json")]) == 0
    capsys.readouterr()
    assert seen["server_name"] == "omni-media-ext"
