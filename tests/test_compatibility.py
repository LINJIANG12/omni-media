"""两条听音通道之间的线上契约（同构分页 + 注册名共存）。

本文件原为「外部模型代读版 与 原生听音版 作为两个包共存」的兼容性测试。
统一成单一包之后，那些用例全部失去了对象——两个发行名、两个包在同一进程里互不串味、
兄弟探测（`cli.detect_native_sibling`）、各自的 `SKILL.md`、`STATUS_SHARED_KEYS`
这套常量声明——已整段删除，不再保留。

保留下来的只有**真正跨通道**的契约，而且都是穿过真实工具入口断言的行为：

1. 两条通道吐出的 `OMNI_STATUS` 注释能被同一段正则解析，`contract_version` 同为 1；
2. 续读字段同名同义（技能侧就是靠这一条在两条通道之间无感切换）；
3. 两个注册名能同时挂进同一个宿主配置而互不覆盖。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest

from omni_media.adapters.registry import ZCodeAdapter, get_adapter
from omni_media.server import create_server

REPO_ROOT = Path(__file__).resolve().parent.parent
_STATUS_RE = re.compile(r"<!-- OMNI_STATUS: (\{.*?\}) -->")

# 两条通道共有的续读字段：技能侧的续读循环只认这几个名字
CONTINUATION_KEYS = {"next_start_time", "next_duration_minutes"}


def _tool(mode: str, name: str, config_path: Optional[Path] = None) -> Callable[..., Any]:
    kwargs: Dict[str, Any] = {"mode": mode}
    if config_path is not None:
        kwargs["config_path"] = str(config_path)
    tools = create_server(**kwargs)._tool_manager._tools
    assert name in tools, f"{mode} 模式未注册 {name}，实际: {sorted(tools)}"
    return tools[name].fn


def _status(text: str) -> Dict[str, Any]:
    match = _STATUS_RE.search(text)
    assert match, text[:400]
    return json.loads(match.group(1))


def _read_native(audio: Path, **kwargs: Any) -> Dict[str, Any]:
    text = asyncio.run(_tool("native", "read_audio")(file_path=str(audio), **kwargs))
    assert isinstance(text, str), "file 通道必须回传文本状态，而不是原生音频块"
    return _status(text)


def _read_ext(audio: Path, config: Path, **kwargs: Any) -> Dict[str, Any]:
    text = asyncio.run(_tool("ext", "read_media", config)(file_path=str(audio), **kwargs))
    assert isinstance(text, str)
    return _status(text)


def _entries(data: Dict[str, Any]) -> Dict[str, Any]:
    """收集宿主配置里「服务名 -> 启动项」（兼容 `mcpServers` 与 `mcp.servers` 两种结构）。"""
    for container in ("mcpServers", "mcp"):
        node = data.get(container)
        if container == "mcp" and isinstance(node, dict):
            node = node.get("servers")
        if isinstance(node, dict):
            return {
                k: v for k, v in node.items() if isinstance(v, dict) and "command" in v
            }
    return {}


# ---------------------------------------------------------------------------
# 1. 同一条状态注释标签
# ---------------------------------------------------------------------------

def test_both_channels_emit_a_parseable_status_comment(stub, audio_m4a: Path, ext_config: Path):
    """同一段正则要能同时吃下两条通道的返回，且版本号一致。"""
    native = _read_native(audio_m4a, output_mode="file")
    ext = _read_ext(audio_m4a, ext_config)

    for status in (native, ext):
        assert status["contract_version"] == 1


# ---------------------------------------------------------------------------
# 2. 续读字段同名同义
# ---------------------------------------------------------------------------

def test_continuation_fields_are_identical_across_channels(
    stub, audio_long_m4a: Path, ext_config: Path
):
    """续读字段必须同名同义——这是「换工具名即可无感切换」的事实基础。

    注意两条通道的**下一卷时长建议**可以不同（原生按 `DEFAULT_SAFE_SLICE_MINUTES` 兜底，
    ext 按配置里的 `slice_minutes`），所以这里断言的是**字段名与语义**一致，
    而不是数值相等：`next_start_time` 必须指向同一个断点，两者都必须给出正的下一卷预算。
    """
    native = _read_native(audio_long_m4a, output_mode="file", duration_minutes=0.5)
    ext = _read_ext(audio_long_m4a, ext_config, duration_minutes=0.5)

    for status in (native, ext):
        missing = CONTINUATION_KEYS - set(status)
        assert not missing, f"缺续读字段: {sorted(missing)}"
        assert status["is_finished"] is False
        assert status["next_start_time"], "未读完必须给出下一卷起点"
        assert float(status["next_duration_minutes"]) > 0

    # 断点必须一致，否则同一门课在两条通道下会读串
    assert native["next_start_time"] == ext["next_start_time"] == "00:00:30"


def test_finished_slice_clears_continuation_on_both_channels(
    stub, audio_m4a: Path, ext_config: Path
):
    native = _read_native(audio_m4a, output_mode="file", duration_minutes=10.0)
    ext = _read_ext(audio_m4a, ext_config, duration_minutes=10.0)

    for status in (native, ext):
        assert status["is_finished"] is True
        assert status["next_start_time"] is None
        assert status["next_duration_minutes"] is None


# ---------------------------------------------------------------------------
# 3. 两个注册名共存
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["dsh", "zcode", "opencode", "codex", "antigravity"])
def test_both_registration_names_coexist_in_one_host_config(target: str):
    """同一个宿主的配置里可以同时挂两条通道：注册名不同，各自钉住自己的 `--mode`。"""
    merged = get_adapter(target, server_name="omni-media").build_applied_config({})
    merged = get_adapter(target, server_name="omni-media-ext").build_applied_config(merged)

    entries = _entries(merged)
    assert {"omni-media", "omni-media-ext"} <= set(entries), f"{target}: 条目缺失 -> {entries}"
    # 每个条目必须钉住自己的模式，否则宿主看到的是 all 模式，通道就分不出来了
    assert entries["omni-media"]["args"][-1] == "native"
    assert entries["omni-media-ext"]["args"][-1] == "ext"

    # 重复挂载必须幂等：再挂一次不改变结果
    again = get_adapter(target, server_name="omni-media-ext").build_applied_config(merged)
    assert _entries(again) == entries


def test_zcode_entry_lands_under_nested_mcp_servers(tmp_path: Path):
    """ZCode 用户配置的服务器容器是 `mcp.servers`，不是顶层 `mcpServers`。

    写错层级 ZCode 一个条目都读不到（它把顶层 `mcpServers` 认作 `.agents/mcp.json`
    那份兜底文件的形状），而 `apply` 依然打印成功——所以层级本身要有断言钉住。
    """
    host_config = tmp_path / "config.json"
    host_config.write_text(json.dumps({"plugins": {"enabledPlugins": {}}}), encoding="utf-8")

    adapter = get_adapter("zcode", custom_config_path=host_config, server_name="omni-media")
    assert adapter.is_registered() is False
    adapter.write_config(adapter.build_applied_config(adapter.read_config()))

    written = json.loads(host_config.read_text(encoding="utf-8"))
    assert "mcpServers" not in written, "顶层 mcpServers 对 ZCode 无效，必须写进 mcp.servers"
    entry = written["mcp"]["servers"]["omni-media"]
    assert entry["args"][-1] == "native"
    assert entry["type"] == "stdio"
    # 默认 30s 撑不过一次切片转录，超时必须显式下发
    assert entry["timeoutMs"] == ZCodeAdapter.TIMEOUT_MS > 30000
    assert written["plugins"] == {"enabledPlugins": {}}, "无关字段必须原样保留"
    assert adapter.is_registered() is True

    adapter.write_config(adapter.build_unapplied_config(adapter.read_config()))
    assert get_adapter("zcode", custom_config_path=host_config).is_registered() is False


def test_other_hosts_keep_top_level_mcp_servers(tmp_path: Path):
    """层级是宿主私有属性：除 ZCode 外的宿主仍写顶层 `mcpServers`。"""
    for target in ("codex", "dsh", "opencode", "antigravity"):
        host_config = tmp_path / f"{target}.json"
        adapter = get_adapter(target, custom_config_path=host_config)
        merged = adapter.build_applied_config({})
        assert "mcpServers" in merged, target
        adapter.write_config(merged)
        assert adapter.is_registered() is True, target
