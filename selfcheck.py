#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OmniMedia Unified Architecture Self-Check."""

import asyncio
import sys
from pathlib import Path

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception as err:
        FAILURES.append((name, err))
        print(f"[FAIL] {name}: {type(err).__name__}: {err}")


def check_imports():
    import omni_media
    import omni_media.cli
    import omni_media.config
    import omni_media.core.inspector
    import omni_media.core.limits
    import omni_media.core.preprocessor
    import omni_media.core.proc
    import omni_media.core.temp_manager
    import omni_media.providers.base
    import omni_media.providers.gemini
    import omni_media.providers.openai
    import omni_media.providers.registry
    import omni_media.adapters.base
    import omni_media.adapters.registry
    import omni_media.server


def check_tools_and_modes():
    from omni_media.server import create_server

    # Mode: all
    server_all = create_server(mode="all")
    tools_all = set(server_all._tool_manager._tools.keys())
    assert tools_all == {"inspect_media", "read_audio", "read_media"}, f"Expected all tools, got {tools_all}"

    # Mode: native
    server_nat = create_server(mode="native")
    tools_nat = set(server_nat._tool_manager._tools.keys())
    assert tools_nat == {"inspect_media", "read_audio"}, f"Expected native tools, got {tools_nat}"

    # Mode: ext
    server_ext = create_server(mode="ext")
    tools_ext = set(server_ext._tool_manager._tools.keys())
    assert tools_ext == {"inspect_media", "read_media"}, f"Expected ext tools, got {tools_ext}"

    # Check annotations
    t_inspect = server_all._tool_manager._tools["inspect_media"]
    t_audio = server_all._tool_manager._tools["read_audio"]
    t_media = server_all._tool_manager._tools["read_media"]

    assert getattr(t_inspect.annotations, "read_only_hint", None) is True
    assert getattr(t_inspect.annotations, "open_world_hint", None) is False

    assert getattr(t_audio.annotations, "read_only_hint", None) is True
    assert getattr(t_audio.annotations, "open_world_hint", None) is False

    assert getattr(t_media.annotations, "read_only_hint", None) is True
    assert getattr(t_media.annotations, "open_world_hint", None) is True
    # Verify output_file parameter is present in read_media tool
    tool_props = getattr(t_media, "parameters", {}).get("properties", {})
    assert "output_file" in tool_props, f"output_file missing from read_media parameters: {tool_props.keys()}"


def check_adapters():
    from omni_media.adapters.registry import get_adapter, list_supported_targets
    targets = list_supported_targets()
    expected = {"antigravity", "codex", "dsh", "opencode", "zcode"}
    assert set(targets) == expected, f"Targets mismatch: {targets}"

    for t in targets:
        adp = get_adapter(t, server_name="omni-media")
        entry = adp.build_entry()
        assert entry["command"] == sys.executable
        assert "-m" in entry["args"]
        cfg_path = adp.get_config_path()
        assert isinstance(cfg_path, Path)


def check_temp_manager():
    from omni_media.core.temp_manager import ManagedTempDir
    target_dir = None
    with ManagedTempDir(prefix="test_omni_") as p:
        target_dir = p
        assert p.exists()
        (p / "dummy.txt").write_text("ok", encoding="utf-8")
    assert not target_dir.exists(), "ManagedTempDir failed to auto-clean"


def check_cli_parser():
    from omni_media.cli import build_parser
    parser = build_parser()
    subcommands = parser._subparsers._actions[1].choices
    for cmd in ("serve", "status", "inspect", "print-config", "apply", "unapply", "config"):
        assert cmd in subcommands, f"Missing CLI subcommand: {cmd}"


def main():
    print("==============================================================")
    print("OmniMedia 统一架构自检")
    print(f"  代码根: {REPO_ROOT}")
    print(f"  Python: {sys.version.split()[0]}")
    print("==============================================================")

    check("核心模块与统一入口导入", check_imports)
    check("MCP 工具面与模式切换 (all / native / ext)", check_tools_and_modes)
    check("宿主适配器注册与配置生成", check_adapters)
    check("资源临时目录与自清理安全", check_temp_manager)
    check("CLI 命令集与参数解析", check_cli_parser)

    print("==============================================================")
    if FAILURES:
        print(f"[FAIL] {len(FAILURES)} 项检查失败")
        sys.exit(1)
    else:
        print("[OK] 全部自检通过")
        sys.exit(0)


if __name__ == "__main__":
    main()
