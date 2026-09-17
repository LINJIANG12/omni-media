#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""omni-media-mcp 独立仓库自检（三域分离后的 MCP 侧）。

与技能仓库完全解耦：本文件只依赖本仓库内的 `omni_media_mcp` 包，不引用、不导入
技能仓库的 `src`。覆盖的不变量：

1. 模块导入与 CLI 入口可用（`python -m omni_media_mcp.cli status` 退出码 0）；
2. 资源安全 limits 齐备（探测超时、内联体积上限、One-Shot 阈值、ffmpeg 并发上限）；
3. 工具契约：非法入参抛类型化异常（FileNotFoundError / ValueError）；
4. 只暴露 `read_audio` / `inspect_media`，废弃的云端委托链路（read_media/ask_media/probe_models
   与 provider / benchmark / installer / prompts 层）必须不复存在；
5. 五个宿主适配器与通用 `print-config` 共用标准 stdio 构造器，且不带任何 API Key；
6. Codex 适配器默认把技能写到**用户级** `~/.agents/skills/`，不污染当前工作目录；
7. 源码无硬编码本机绝对路径（AST 取字符串常量，跳过文档字符串里的示例）。

Run: python selfcheck.py
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

MCP_ROOT = Path(__file__).resolve().parent
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from omni_media_mcp.core.proc import run_quiet  # noqa: E402  （统一抑制 Windows 控制台窗口）

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception as err:
        FAILURES.append((name, err))
        print(f"[FAIL] {name}: {type(err).__name__}: {err}")


def check_imports_and_cli():
    import omni_media_mcp
    import omni_media_mcp.server  # noqa: F401
    from omni_media_mcp.core.preprocessor import MediaPreprocessor  # noqa: F401
    from omni_media_mcp.adapters.registry import list_supported_targets

    assert omni_media_mcp.__version__ == "0.2.0", f"版本号异常: {omni_media_mcp.__version__}"
    pyproject = (MCP_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.2.0"' in pyproject
    assert '"mcp>=2.1.0,<3"' in pyproject
    assert 'omni_media_mcp = ["skills/**/*.md"]' in pyproject

    res = run_quiet(
        [sys.executable, "-m", "omni_media_mcp.cli", "status"],
        cwd=str(MCP_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
    )
    assert res.returncode == 0, f"`omni-media status` 退出码 {res.returncode}: {(res.stdout or '')[-300:]}"
    assert len(list_supported_targets()) >= 5, f"宿主适配器数量异常: {list_supported_targets()}"


def check_limits():
    from omni_media_mcp.core.limits import (
        AUDIO_BITRATE_VOICE,
        MAX_CONCURRENT_FFMPEG,
        MAX_INLINE_BYTES,
        MAX_ONESHOT_MINUTES,
        MAX_SAFE_INLINE_BYTES,
        MEDIA_EXTS,
        OUTPUT_MODE_WHITELIST,
        PROBE_TIMEOUT_SEC,
        SUBPROCESS_TIMEOUT_SEC,
    )

    assert PROBE_TIMEOUT_SEC > 0 and SUBPROCESS_TIMEOUT_SEC > 0, "子进程超时未设置"
    assert MAX_CONCURRENT_FFMPEG >= 1, "ffmpeg 并发上限异常"
    assert MAX_ONESHOT_MINUTES > 0, "One-Shot 阈值异常"
    assert MAX_SAFE_INLINE_BYTES < MAX_INLINE_BYTES, "内联安全阈值应严格小于硬上限"
    assert OUTPUT_MODE_WHITELIST == frozenset({"auto", "file", "inline"}), "output_mode 白名单被改动"
    assert ".m4a" in MEDIA_EXTS and ".mp4" in MEDIA_EXTS, "媒体扩展名白名单缺项"
    assert AUDIO_BITRATE_VOICE.endswith("k"), "人声码率常量异常"


def check_tool_contract():
    """非法入参必须抛类型化异常，且废弃的云端委托链路不得回流。"""
    from omni_media_mcp import server

    for gone in ("read_media", "ask_media", "probe_models"):
        assert not hasattr(server, gone), f"{gone} 属废弃的云端委托链路，应已移除"
    for live in ("read_audio", "inspect_media"):
        assert hasattr(server, live), f"{live} 是当前唯一入口，不得缺失"

    missing = str(MCP_ROOT / "definitely-missing.m4a")
    non_media = str(MCP_ROOT / "pyproject.toml")

    async def _run():
        cases = [
            (server.read_audio(file_path=missing), FileNotFoundError),
            (server.read_audio(file_path=non_media), ValueError),
            (server.read_audio(file_path=non_media, output_mode="bogus"), ValueError),
            (server.inspect_media(file_path=missing), FileNotFoundError),
            (server.inspect_media(file_path=non_media), ValueError),
        ]
        for coro, expected in cases:
            try:
                await coro
            except expected:
                continue
            except Exception as err:
                raise AssertionError(f"期望 {expected.__name__}，实际 {type(err).__name__}: {err}")
            raise AssertionError(f"非法入参未抛出 {expected.__name__}")

    asyncio.run(_run())


def check_tool_annotations():
    """工具必须配置完整的 ToolAnnotations 提示（四大提示均为布尔值）。"""
    from omni_media_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("read_audio", "inspect_media"):
        assert name in tools, f"工具 {name} 未注册"
        ann = tools[name].annotations
        assert ann is not None, f"工具 {name} 缺少 ToolAnnotations"
        assert isinstance(ann.read_only_hint, bool), f"{name}.readOnlyHint 必须是 bool"
        assert isinstance(ann.destructive_hint, bool), f"{name}.destructiveHint 必须是 bool"
        assert isinstance(ann.idempotent_hint, bool), f"{name}.idempotentHint 必须是 bool"
        assert isinstance(ann.open_world_hint, bool), f"{name}.openWorldHint 必须是 bool"
        assert ann.read_only_hint is True, f"{name}.readOnlyHint 应为 True"
        assert ann.destructive_hint is False, f"{name}.destructiveHint 应为 False"
        assert ann.idempotent_hint is True, f"{name}.idempotentHint 应为 True"
        assert ann.open_world_hint is False, f"{name}.openWorldHint 应为 False"


def check_dead_layers_removed():
    for rel in (
        "omni_media_mcp/installer.py",
        "omni_media_mcp/providers",
        "omni_media_mcp/benchmarks",
        "omni_media_mcp/prompts.py",
    ):
        assert not (MCP_ROOT / rel).exists(), f"{rel} 应已删除"


def check_generic_config():
    """通用配置、适配器和 print-config 必须共建同一条 stdio 入口。"""
    import json

    from omni_media_mcp.adapters.base import build_generic_config
    from omni_media_mcp.adapters.codex import CodexAdapter

    generic = build_generic_config()
    entry = generic["mcpServers"]["omni-media"]
    assert entry["args"] == ["-m", "omni_media_mcp.server"]
    assert Path(entry["env"]["PYTHONPATH"]) == MCP_ROOT
    assert CodexAdapter().build_entry()["args"] == entry["args"]

    res = run_quiet(
        [sys.executable, "-m", "omni_media_mcp.cli", "print-config"],
        cwd=str(MCP_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == generic


def check_adapters_are_credential_free():
    """适配器只注入 PYTHONPATH；任何 *_API_KEY 都不得出现在 build_entry() 结果里。"""
    from omni_media_mcp.adapters.registry import ADAPTER_MAP, get_adapter

    for target in ADAPTER_MAP:
        entry = get_adapter(target).build_entry()
        assert entry.get("command"), f"{target} 适配器缺少启动命令"
        assert entry.get("args"), f"{target} 适配器缺少启动参数"
        env = entry.get("env") or {}
        assert "PYTHONPATH" in env, f"{target} 适配器未注入 PYTHONPATH"
        leaked = [k for k in env if k.upper().endswith("API_KEY") or "TOKEN" in k.upper()]
        assert not leaked, f"{target} 适配器注入了凭证类环境变量: {leaked}"


def check_codex_skill_not_writing_cwd():
    """Codex 适配器默认写用户级技能目录，不得把技能文件写进当前工作目录（跨仓库串扰）。"""
    from omni_media_mcp.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    target = adapter.get_skill_target_path()
    assert Path.home() in target.parents, f"默认技能路径不在用户目录下: {target}"
    assert MCP_ROOT not in target.parents, f"默认技能路径落在本仓库内: {target}"

    custom = CodexAdapter(custom_skill_dir=MCP_ROOT / "__probe_skill_dir__")
    assert Path(custom.get_skill_target_path()).parent.name == "__probe_skill_dir__", \
        "显式 --skill-dir 未生效"


def check_no_hardcoded_machine_paths():
    """源码不得硬编码本机盘符绝对路径（跳过文档字符串里的示例路径）。"""
    drive_re = re.compile(r"(^|[^\w])[A-Za-z]:[\\/]")

    def _docstring_nodes(tree: ast.AST) -> set:
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    found.add(id(body[0].value))
        return found

    targets = [p for p in MCP_ROOT.rglob("*.py") if "__pycache__" not in p.parts and p.name != "selfcheck.py"]
    assert targets, "未找到任何源码文件，扫描范围异常"

    hits = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                if drive_re.search(node.value):
                    hits.append(f"{path.relative_to(MCP_ROOT).as_posix()}:{node.lineno}: {node.value[:70]}")
    assert not hits, "源码内存在硬编码本机绝对路径:\n      " + "\n      ".join(hits)


def check_no_console_window_spawn():
    """所有外部程序调用必须走 run_quiet（Windows 下抑制控制台窗口）且带硬超时。

    背景：ffmpeg/ffprobe 每次调用都新建进程；宿主后台托管 + 多子智能体并发时
    Windows 会为每个控制台程序新开窗口（成片闪黑窗）。窗口抑制集中在 core/proc.py。
    """
    from omni_media_mcp.core import proc as proc_mod

    assert hasattr(proc_mod, "run_quiet") and hasattr(proc_mod, "CREATE_NO_WINDOW"), "core/proc.py 缺少 run_quiet"
    if os.name == "nt":
        assert proc_mod.quiet_kwargs().get("creationflags") == proc_mod.CREATE_NO_WINDOW and proc_mod.CREATE_NO_WINDOW, \
            "run_quiet 在 Windows 下未启用 CREATE_NO_WINDOW"

    bare = []
    timeout_missing = []
    for path in list(MCP_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "proc.py":
            continue  # proc.py 是唯一允许直接调用 subprocess.run 的地方
        rel = path.relative_to(MCP_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if getattr(func, "attr", None) in {"run", "Popen", "call", "check_output"} \
                    and getattr(getattr(func, "value", None), "id", None) in {"subprocess", "_sp"}:
                bare.append(f"{rel}:{node.lineno}")
            if getattr(func, "id", None) == "run_quiet":
                if not any(kw.arg == "timeout" for kw in node.keywords):
                    timeout_missing.append(f"{rel}:{node.lineno}")
    assert not bare, "存在未抑制控制台窗口的裸子进程调用: " + ", ".join(bare)
    assert not timeout_missing, f"run_quiet 调用缺 timeout=: {timeout_missing}"


def check_no_skill_repo_imports():
    """互不打扰：MCP 代码不得 import 技能仓库的 src。"""
    bad = []
    for path in (MCP_ROOT / "omni_media_mcp").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name.split(".")[0] == "src" for a in node.names):
                    bad.append(path.relative_to(MCP_ROOT).as_posix())
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] == "src":
                    bad.append(path.relative_to(MCP_ROOT).as_posix())
    assert not bad, f"MCP 侧不得 import 技能仓库 src: {sorted(set(bad))}"


def main() -> int:
    print("=" * 62)
    print("omni-media-mcp 独立仓库自检")
    print(f"  仓库根: {MCP_ROOT}")
    print(f"  Python: {sys.version.split()[0]}")
    print("=" * 62)
    check("模块导入与 CLI 入口", check_imports_and_cli)
    check("资源安全 limits 齐备", check_limits)
    check("工具契约（read_audio / inspect_media）", check_tool_contract)
    check("工具提示注解（ToolAnnotations 四大提示）", check_tool_annotations)
    check("废弃云委托层已移除", check_dead_layers_removed)
    check("宿主适配器零凭证 + PYTHONPATH", check_adapters_are_credential_free)
    check("通用 print-config 与适配器同源", check_generic_config)
    check("Codex 适配器不写入当前工作目录", check_codex_skill_not_writing_cwd)
    check("源码无硬编码本机路径", check_no_hardcoded_machine_paths)
    check("子进程统一走 run_quiet（无控制台弹窗）", check_no_console_window_spawn)
    check("不 import 技能仓库 src", check_no_skill_repo_imports)
    print("=" * 62)
    if FAILURES:
        print(f"[FAILED] {len(FAILURES)} 项未通过:")
        for name, err in FAILURES:
            print(f"  - {name}: {err}")
        return 1
    print("[OK] 全部自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
