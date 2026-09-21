"""统一包的模式面契约：一个包、一个启动模块、两条通道靠 `--mode` 区分。

替代旧 `mcp-ext/tests/test_compatibility.py`。旧文件的前提是「两个独立子包并存」——
包名/发行名/控制台命令/MCP 注册键/配置文件全部分开、互不 import、任一方缺席都不影响
另一方。统一后这套前提**整体失效**，因此该文件不复存在；其中仍然成立、且值得继续钉住的
契约搬到这里，并改成「同一进程内按 mode 参数化」的形式：

1. 工具面由 mode 决定：`native` 只出 `read_audio`，`ext` 只出 `read_media`，`all` 两者都出；
2. 注册名 → mode 的映射由 `SERVER_MODES` 单点决定，宿主编排出来的 `--mode` 必须跟注册名走；
3. 四个启动器各自绑定唯一实现（过去四个入口指向同一个 `server:main`，工具列表上分不出通道）；
4. 切片三件套（file_path / start_time / duration_minutes）两条通道同名同型，调用方可直接迁移；
5. 四个 ToolAnnotations 提示齐全且都是显式布尔值。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from omni_media.adapters.base import (
    DEFAULT_SERVER_MODE,
    SERVER_MODES,
    build_generic_config,
    mode_for_server,
)
from omni_media.adapters.registry import get_adapter, list_supported_targets
from omni_media.server import create_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

ALL_MODES = ("all", "native", "ext")

EXPECTED_SURFACE = {
    "all": {"inspect_media", "read_audio", "read_media"},
    "native": {"inspect_media", "read_audio"},
    "ext": {"inspect_media", "read_media"},
}

# 四个 ToolAnnotations 提示的期望值：read_media 会连外部世界（openWorldHint=True）
# 且传 `output_file` 时会写盘（readOnlyHint=False，见第二阶段 B4）；
# 其余两个都是纯本地/零网络的只读工具。
EXPECTED_HINTS = {
    "inspect_media": (True, False, True, False),
    "read_audio": (True, False, True, False),
    "read_media": (False, False, True, True),
}


def _tools(mode: str) -> Dict[str, Any]:
    return create_server(mode=mode)._tool_manager._tools


def _json_types(prop: dict) -> Set[str]:
    """取参数 JSON Schema 的类型集合（Optional 参数会是 anyOf，没有顶层 type）。"""
    if "type" in prop:
        return {prop["type"]}
    types: Set[str] = set()
    for branch in list(prop.get("anyOf", [])) + list(prop.get("oneOf", [])):
        if isinstance(branch, dict) and "type" in branch:
            types.add(branch["type"])
    return types


# ---------------------------------------------------------------------------
# 1. 工具面由 mode 决定
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ALL_MODES)
def test_tool_surface_follows_mode(mode: str):
    assert set(_tools(mode)) == EXPECTED_SURFACE[mode]


@pytest.mark.parametrize("mode", ALL_MODES)
def test_every_registered_tool_declares_the_four_hints(mode: str):
    for name, tool in _tools(mode).items():
        ann = tool.annotations
        assert ann is not None, f"{mode}:{name} 缺少 annotations"
        dump = ann.model_dump(by_alias=True)
        for hint_key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert hint_key in dump, f"{mode}:{name} 缺少 {hint_key}"
            assert isinstance(dump[hint_key], bool), f"{mode}:{name} 的 {hint_key} 不是 bool: {dump[hint_key]}"

        ro, dest, idem, ow = EXPECTED_HINTS[name]
        assert ann.read_only_hint is ro, f"{mode}:{name} readOnlyHint 应为 {ro}"
        assert ann.destructive_hint is dest, f"{mode}:{name} destructiveHint 应为 {dest}"
        assert ann.idempotent_hint is idem, f"{mode}:{name} idempotentHint 应为 {idem}"
        assert ann.open_world_hint is ow, f"{mode}:{name} openWorldHint 应为 {ow}"


def test_illegal_mode_silently_degrades_to_local_only_and_is_reported():
    """现状记录：非法 mode 不报错，只注册本地 `inspect_media`（已作为问题上报）。

    钉住它不是认可它，而是让「悄悄降级成半个服务」这种事在改动时立刻可见——
    `_serve()` 靠 argparse 的 choices 兜住了入口，但直接调 `create_server()` 的调用方没有这层保护。
    """
    assert set(_tools("telepathy")) == {"inspect_media"}


# ---------------------------------------------------------------------------
# 2. 注册名 → mode 的映射
# ---------------------------------------------------------------------------

def test_server_mode_map_is_the_single_source_of_truth():
    assert SERVER_MODES == {"omni-media": "native", "omni-media-ext": "ext"}
    assert DEFAULT_SERVER_MODE == "all"

    assert mode_for_server("omni-media") == "native"
    assert mode_for_server("omni-media-ext") == "ext"
    assert mode_for_server("  omni-media-ext  ") == "ext"  # 容忍首尾空白
    assert mode_for_server("my-own-name") == "all", "自定义注册名必须退回 all，保持可用"


def test_unknown_registration_name_generates_all_mode_entry():
    entry = build_generic_config(server_name="my-own-name")["mcpServers"]["my-own-name"]
    assert entry["args"] == ["-m", "omni_media.server", "--mode", "all"]


@pytest.mark.parametrize("server_name, expected_mode", [("omni-media", "native"), ("omni-media-ext", "ext")])
def test_generic_config_carries_mode_for_registration_name(server_name: str, expected_mode: str):
    cfg = build_generic_config(server_name=server_name)
    assert set(cfg["mcpServers"]) == {server_name}
    entry = cfg["mcpServers"][server_name]
    assert entry["args"] == ["-m", "omni_media.server", "--mode", expected_mode]
    assert set(entry["env"]) == {"PYTHONPATH"}


@pytest.mark.parametrize("target", sorted(list_supported_targets()))
@pytest.mark.parametrize("server_name, expected_mode", [("omni-media", "native"), ("omni-media-ext", "ext")])
def test_every_host_adapter_launches_the_mode_matching_its_registration_name(
    target: str, server_name: str, expected_mode: str
):
    """适配器不能自己挑 mode：它必须由注册名推导，否则宿主工具列表上看不出装的是哪条通道。"""
    entry = get_adapter(target, server_name=server_name).build_entry()
    assert entry["args"] == ["-m", "omni_media.server", "--mode", expected_mode], target


# ---------------------------------------------------------------------------
# 3. 四个启动器各自绑定唯一实现
# ---------------------------------------------------------------------------

def test_pyproject_binds_four_distinct_launchers():
    assert 'name = "omni-media"' in PYPROJECT
    assert 'omni-media = "omni_media.cli:main"' in PYPROJECT
    assert 'omni-media-ext = "omni_media.cli:main_ext"' in PYPROJECT
    assert 'omni-media-mcp = "omni_media.server:main_native"' in PYPROJECT
    assert 'omni-media-ext-mcp = "omni_media.server:main_ext"' in PYPROJECT

    targets = [
        "omni_media.cli:main",
        "omni_media.cli:main_ext",
        "omni_media.server:main_native",
        "omni_media.server:main_ext",
    ]
    assert len(set(targets)) == 4, "四个启动器必须指向四个不同实现"


def test_cli_entry_points_report_their_own_registration_name(monkeypatch):
    from omni_media import cli

    seen: list[str] = []
    monkeypatch.setattr(cli, "_run", lambda argv, server_name: seen.append(server_name) or 0)

    assert cli.main([]) == 0
    assert cli.main_ext([]) == 0
    assert seen == ["omni-media", "omni-media-ext"]


def test_server_entry_points_default_to_the_matching_mode(monkeypatch):
    import omni_media.server as server_mod

    modes: list[str] = []
    monkeypatch.setattr(server_mod, "_serve", lambda default_mode: modes.append(default_mode))

    server_mod.main()
    server_mod.main_native()
    server_mod.main_ext()
    assert modes == ["all", "native", "ext"]


def test_serve_flag_overrides_entry_default(monkeypatch):
    """入口只提供默认值，`--mode` 仍可显式覆盖（人工排查时用得上）。"""
    import omni_media.server as server_mod

    captured: Dict[str, Any] = {}

    class _FakeServer:
        def run(self, transport: str) -> None:
            captured["transport"] = transport

    def fake_create_server(mode: str = "all", config_path: Any = None):
        captured["mode"] = mode
        return _FakeServer()

    monkeypatch.setattr(server_mod, "create_server", fake_create_server)

    monkeypatch.setattr(sys, "argv", ["prog"])
    server_mod._serve("ext")
    assert captured == {"mode": "ext", "transport": "stdio"}

    monkeypatch.setattr(sys, "argv", ["prog", "--mode", "native"])
    server_mod._serve("ext")
    assert captured["mode"] == "native"

    monkeypatch.setattr(sys, "argv", ["prog", "--mode", "telepathy"])
    with pytest.raises(SystemExit):
        server_mod._serve("ext")


def test_all_launchers_share_one_server_module():
    """区分通道的是 `--mode`，不是模块名：统一后只剩一个启动模块。"""
    assert "omni_media.server" in PYPROJECT
    # 旧的两个启动模块名不得再出现（它们随子包目录一起被删除）
    assert "omni_media_mcp" not in PYPROJECT
    assert "omni_media_ext" not in PYPROJECT


# ---------------------------------------------------------------------------
# 4. 切片参数两条通道同名同型
# ---------------------------------------------------------------------------

def test_slicing_trio_is_identical_across_both_tool_surfaces():
    tools = _tools("all")
    audio_schema = tools["read_audio"].parameters
    media_schema = tools["read_media"].parameters
    audio_props = audio_schema["properties"]
    media_props = media_schema["properties"]

    for key in ("file_path", "start_time", "duration_minutes"):
        assert key in audio_props and key in media_props, key
        assert _json_types(audio_props[key]) == _json_types(media_props[key]), (
            f"{key} 的类型在两条通道之间不一致: "
            f"{_json_types(audio_props[key])} vs {_json_types(media_props[key])}"
        )

    # 两者都以 file_path 为唯一必填项
    assert audio_schema.get("required") == ["file_path"] == media_schema.get("required")


def test_mode_specific_arguments_stay_on_their_own_tool():
    """`output_mode` 是原生通道独有的；`prompt`/`endpoint`/`output_file` 是代读通道独有的。"""
    tools = _tools("all")
    audio_props = set(tools["read_audio"].parameters["properties"])
    media_props = set(tools["read_media"].parameters["properties"])

    assert "output_mode" in audio_props and "output_mode" not in media_props
    for key in ("prompt", "endpoint", "output_file", "mode"):
        assert key in media_props and key not in audio_props


def test_local_probe_is_available_in_every_mode():
    """`inspect_media` 是零网络零凭证的本地能力，任何 mode 都必须有。"""
    for mode in ALL_MODES:
        assert "inspect_media" in _tools(mode)


# ---------------------------------------------------------------------------
# 5. 一个包，没有旧子包残留
# ---------------------------------------------------------------------------

def test_single_package_owns_both_modes():
    import omni_media
    from omni_media import cli, config, server

    assert omni_media.__version__ == "0.4.0"
    assert cli.__name__ == "omni_media.cli"
    assert config.__name__ == "omni_media.config"
    assert server.__name__ == "omni_media.server"

    # 发行名只有一个；旧的两个发行名不得再出现在 pyproject 里
    assert f'version = "{omni_media.__version__}"' in PYPROJECT
    assert 'name = "omni-media-mcp"' not in PYPROJECT
    assert 'name = "omni-media-ext"' not in PYPROJECT


def test_implementation_never_refers_to_the_removed_subpackages():
    """实现层不得再 import 或提及旧包名（`omni_media_mcp` / `omni_media_ext`）。

    旧子包整目录删除后，任何残留引用都会在运行时炸成 ModuleNotFoundError；
    这条静态检查让它在测试里就暴露，而不是等用户启动服务时才发现。
    """
    offenders = []
    for path in (REPO_ROOT / "omni_media").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for legacy in ("omni_media_mcp", "omni_media_ext"):
            if legacy in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {legacy}")
    assert offenders == []
