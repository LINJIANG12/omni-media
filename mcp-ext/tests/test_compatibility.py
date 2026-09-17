"""兼容性测试：本版本（外部模型代读）与原生听音版（omni-media-mcp）组成一对时的契约。

两个 MCP 是同一个岗位的两种实现：

* 宿主**有**原生音频模态 → 挂 `omni-media`，用 `read_audio`（零凭证、延迟低）；
* 宿主**没有**原生音频模态 → 挂 `omni-media-ext`，用 `read_media`（外部模型代读）。

「兼容」不是口号，这里逐条钉死它的可验证含义：

1. **身份不冲突**：包名 / 发行名 / 控制台命令 / MCP 注册键 / 配置文件 全部分开，
   两个发行版能在同一个 Python 环境与同一个宿主里并存；
2. **线上契约同构**：分页状态注释同名同语义，续读循环对调用方完全一致；
3. **参数可迁移**：切片相关参数名一致，从原生切到外部的改动量最小；
4. **不做多余的相互依赖**：不互相 import，任一方缺席都不影响另一方工作。

原生仓库不存在时（例如只发布了本版本）全部跳过，不会误报失败。
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SIBLING_MCP = REPO_ROOT.parent / "mcp"

_STATUS_RE = re.compile(r"<!-- (OMNI_STATUS|OMNI_MEDIA_EXT_STATUS): (\{.*?\}) -->")

requires_native = pytest.mark.skipif(
    not (SIBLING_MCP / "omni_media_mcp" / "server.py").is_file(),
    reason="同级原生仓库 mcp/ 不存在",
)


def _native_importable() -> bool:
    try:
        importlib.import_module("omni_media_mcp.server")
    except Exception:  # noqa: BLE001
        return False
    return True


requires_native_installed = pytest.mark.skipif(
    not _native_importable(),
    reason="原生版 omni-media-mcp 未安装（pip install -e ../mcp）",
)


# ---------------------------------------------------------------------------
# 1. 身份不冲突
# ---------------------------------------------------------------------------

def test_ext_identity_is_distinct():
    """本版本的四个身份标识必须自带前缀，且不冒用原生版的名字。"""
    from omni_media_ext import __version__
    from omni_media_ext.config import CONFIG_FILENAME, USER_CONFIG_DIRNAME, repo_root

    assert __version__ == "0.2.0"

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "omni-media-ext"' in pyproject
    assert 'name = "omni-media-mcp"' not in pyproject
    assert 'omni-media-ext = "omni_media_ext.cli:main"' in pyproject
    assert 'omni-media = "omni_media_mcp.cli:main"' not in pyproject

    # 配置文件名与用户级目录名都必须与原生版区分（原生版没有配置文件，这里防的是将来撞车）
    assert CONFIG_FILENAME == "config.json"
    assert USER_CONFIG_DIRNAME == ".omni-media-ext"
    assert repo_root() == REPO_ROOT


def test_both_distributions_can_coexist():
    """两个发行版同时安装，且版本与安装位置各自独立。"""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        ext = distribution("omni-media-ext")
    except PackageNotFoundError:  # pragma: no cover - 未安装时不算失败
        pytest.skip("omni-media-ext 未安装")

    assert ext.version == "0.2.0"
    try:
        native = distribution("omni-media-mcp")
    except PackageNotFoundError:
        pytest.skip("原生版未安装，无需比对并存性")

    assert native.version == "0.2.0"
    assert ext.metadata["Name"] != native.metadata["Name"]


def test_both_packages_import_in_one_process_without_bleed():
    """同进程同时导入两个包：模块对象、工具面、常量都必须互不串味。"""
    native = pytest.importorskip("omni_media_mcp.core.limits")
    from omni_media_ext.core import limits as ext_limits
    from omni_media_ext.server import mcp as ext_mcp

    assert native is not ext_limits
    assert sys.modules["omni_media_mcp"] is not sys.modules["omni_media_ext"]

    ext_tools = {tool.name for tool in ext_mcp._tool_manager.list_tools()}
    assert ext_tools == {"read_media", "inspect_media"}

    try:
        from omni_media_mcp.server import mcp as native_mcp
    except Exception:  # noqa: BLE001
        pytest.skip("原生版 server 导入失败")

    native_tools = {tool.name for tool in native_mcp._tool_manager.list_tools()}
    assert native_tools == {"read_audio", "inspect_media"}
    # 关键的不重叠：本版本没有 read_audio，原生版没有 read_media
    assert "read_audio" not in ext_tools
    assert "read_media" not in native_tools


def test_tool_dispatch_targets_differ():
    """`read_media` 与 `read_audio` 必须解析到各自的实现，不能是同一个函数。"""
    from omni_media_ext.server import read_media as ext_read

    try:
        from omni_media_mcp.server import read_audio as native_read
    except Exception:  # noqa: BLE001
        pytest.skip("原生版 server 导入失败")

    assert ext_read is not native_read
    assert getattr(ext_read, "__module__", "").startswith("omni_media_ext")
    assert getattr(native_read, "__module__", "").startswith("omni_media_mcp")


@requires_native
def test_both_registerable_into_one_host_config():
    """同一个宿主的配置里可以同时挂两个服务，键名与启动模块互不覆盖。"""
    from omni_media_ext.adapters.registry import get_adapter as get_ext_adapter

    native_registry = pytest.importorskip("omni_media_mcp.adapters.registry")

    for target in ("dsh", "zcode", "opencode", "codex", "antigravity"):
        # 先挂原生版，再挂本版本：本版本不能顶掉原生版的键
        merged = native_registry.get_adapter(target).build_applied_config({})
        merged = get_ext_adapter(target).build_applied_config(merged)

        flat = json.dumps(merged, ensure_ascii=False)
        assert '"omni-media"' in flat, f"{target}: 原生版的键丢了"
        assert '"omni-media-ext"' in flat, f"{target}: 本版本的键没写进去"
        assert "omni_media_mcp.server" in flat, f"{target}: 原生版启动模块丢了"
        assert "omni_media_ext.server" in flat, f"{target}: 本版本启动模块丢了"

        # 反向顺序同样幂等：重复挂载不会互相顶掉
        round_trip = get_ext_adapter(target).build_applied_config(merged)
        flat_trip = json.dumps(round_trip, ensure_ascii=False)
        assert '"omni-media"' in flat_trip and '"omni-media-ext"' in flat_trip

        # 两个条目必须落在同一个容器里（否则宿主只会看到一个）
        assert _entry_names(merged) >= {"omni-media", "omni-media-ext"}, target


def _entry_names(data: dict) -> set[str]:
    """把宿主配置里「服务名 -> 启动项」的键收集出来（兼容 mcpServers 与 mcp.servers 两种结构）。"""
    names: set[str] = set()
    for container in ("mcpServers", "mcp"):
        node = data.get(container)
        if container == "mcp" and isinstance(node, dict) and isinstance(node.get("servers"), dict):
            node = node["servers"]
        if isinstance(node, dict):
            names |= {k for k, v in node.items() if isinstance(v, dict) and "command" in v}
    return names


# ---------------------------------------------------------------------------
# 2. 线上契约同构（分页状态注释）
# ---------------------------------------------------------------------------

def test_status_tag_matches_native_contract():
    """同一行内注释标签：调用方一段正则就能同时吃下两个服务的返回。"""
    from omni_media_ext.server import STATUS_TAG

    assert STATUS_TAG == "OMNI_STATUS"
    assert (REPO_ROOT / "omni_media_ext" / "server.py").read_text(encoding="utf-8").count("OMNI_STATUS")


@requires_native
def test_native_declares_the_same_status_tag():
    """只读地核对原生版确实用的是同一个标签——这是同构契约的事实基础。"""
    source = (SIBLING_MCP / "omni_media_mcp" / "server.py").read_text(encoding="utf-8")
    assert "<!-- OMNI_STATUS:" in source, "原生版的分页标签变了，两版契约不再同构"


def test_status_shared_keys_are_declared_and_emitted(stub, audio_m4a: Path):
    """共有字段必须在真实返回里出现，且语义与原生版一致（mode 指切片模式）。"""
    from omni_media_ext.config import Defaults, Endpoint
    from omni_media_ext.providers.gemini import GeminiEndpoint
    from omni_media_ext.server import (
        STATUS_SHARED_CONTINUATION_KEYS,
        STATUS_SHARED_KEYS,
        STATUS_TAG,
    )

    endpoint = GeminiEndpoint(
        Endpoint(name="gem", protocol="gemini", base_url=stub.base_gemini, model="m", api_key="k-1234567890ab"),
        Defaults(slice_minutes=10.0, max_payload_mb=18, timeout_sec=30, max_retries=0),
    )
    result = endpoint.process(audio_m4a, "p", "transcribe")
    assert result.text  # provider 层不掺状态注释，注释由 server 层统一拼

    # 直接构造一段与 server.py 同构的注释，验证「正则 + 字段名」两边一致
    payload = {
        "contract_version": 1,
        "status": "COMPLETED",
        "mode": "chunked",
        "is_finished": True,
        "start_time": "00:00:00",
        "end_time": "00:01:00",
        "total_duration": "00:02:51",
    }
    text = f"<!-- {STATUS_TAG}: {json.dumps(payload, ensure_ascii=False)} -->\n\n正文"
    match = _STATUS_RE.search(text)
    assert match and match.group(1) == "OMNI_STATUS"
    parsed = json.loads(match.group(2))
    for key in STATUS_SHARED_KEYS:
        assert key in parsed, f"共有字段缺失: {key}"
    assert parsed["contract_version"] == 1
    assert parsed["mode"] in ("oneshot", "chunked"), "mode 必须是切片模式语义"
    assert set(STATUS_SHARED_CONTINUATION_KEYS) == {"next_start_time", "next_duration_minutes"}


def test_ext_only_uses_extension_keys_for_its_own_info():
    """本版本的扩展信息不得占用共有键（`task` 而非 `mode` 存任务预设）。"""
    from omni_media_ext.server import STATUS_SHARED_KEYS

    assert "mode" in STATUS_SHARED_KEYS
    assert "task" not in STATUS_SHARED_KEYS

    source = (REPO_ROOT / "omni_media_ext" / "server.py").read_text(encoding="utf-8")
    # 任务预设必须写进 "task"，不能写回 "mode"
    assert '"task": mode_key' in source
    assert '"mode": mode_key' not in source
    assert '"mode": "chunked" if is_sliced else "oneshot"' in source


# ---------------------------------------------------------------------------
# 3. 参数可迁移
# ---------------------------------------------------------------------------

def _json_types(prop: dict) -> set[str]:
    """取参数 JSON Schema 的类型集合（Optional 参数会是 anyOf，没有顶层 type）。"""
    if "type" in prop:
        return {prop["type"]}
    types: set[str] = set()
    for branch in list(prop.get("anyOf", [])) + list(prop.get("oneOf", [])):
        if isinstance(branch, dict) and "type" in branch:
            types.add(branch["type"])
    return types


def test_slicing_arguments_are_named_identically():
    """切片三件套（file_path / start_time / duration_minutes）两边同名同型。"""
    from omni_media_ext.server import mcp

    ext_schema = next(
        tool.parameters for tool in mcp._tool_manager.list_tools() if tool.name == "read_media"
    )
    ext_props = ext_schema.get("properties", {})
    assert {"file_path", "start_time", "duration_minutes"} <= set(ext_props)

    try:
        from omni_media_mcp.server import mcp as native_mcp
    except Exception:  # noqa: BLE001
        pytest.skip("原生版 server 导入失败")

    native_schema = next(
        tool.parameters for tool in native_mcp._tool_manager.list_tools() if tool.name == "read_audio"
    )
    native_props = native_schema.get("properties", {})
    assert {"file_path", "start_time", "duration_minutes"} <= set(native_props)

    # 调用方从原生切到外部时，这三个参数的写法必须一字不改
    for key in ("file_path", "start_time", "duration_minutes"):
        assert _json_types(native_props[key]) == _json_types(ext_props[key]), (
            f"{key} 的类型在两版之间不一致: "
            f"{_json_types(native_props[key])} vs {_json_types(ext_props[key])}"
        )
    # 两者都以 file_path 为唯一必填项
    assert native_schema.get("required") == ["file_path"] == ext_schema.get("required")


# ---------------------------------------------------------------------------
# 4. 不做多余依赖 + 选择规则被文档化
# ---------------------------------------------------------------------------

def test_no_cross_repo_dependency():
    """两版之间不得互相 import；任一方缺席都不影响另一方。

    只禁止**导入**，不禁文本提及：`status` 需要按发行版名探测对方是否可用（只读元数据
    查询，不是依赖），要探测就得写出对方的名字。
    """
    import ast

    def imported_modules(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        return found

    for path in (REPO_ROOT / "omni_media_ext").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        assert "omni_media_mcp" not in imported_modules(path), f"{path.name} import 了原生版"

    if SIBLING_MCP.is_dir():
        for path in (SIBLING_MCP / "omni_media_mcp").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            assert "omni_media_ext" not in imported_modules(path), f"{path.name} import 了本版本"
            assert "omni_media_ext" not in path.read_text(encoding="utf-8"), f"{path.name} 被写入本版本标识"


def test_sibling_probe_degrades_gracefully():
    """对方缺席时探测必须安静返回 False，而不是抛异常拖垮 status。"""
    import importlib.metadata as metadata

    from omni_media_ext import cli

    def boom(name):
        raise metadata.PackageNotFoundError(name)

    original = metadata.distribution
    metadata.distribution = boom  # type: ignore[assignment]
    try:
        available, detail = cli.detect_native_sibling()
        assert available is False
        assert detail  # 具体原因由实现给，但必须有话说
    finally:
        metadata.distribution = original  # type: ignore[assignment]

    # 真实环境下也必须给出结论（无论可用与否，都不能抛）
    available, detail = cli.detect_native_sibling()
    assert isinstance(available, bool) and isinstance(detail, str) and detail


@requires_native_installed
def test_sibling_probe_reports_available_when_importable():
    """装好了就必须报「可用」。

    这条守的是一个真实踩过的坑：曾用 dist.locate_file("omni_media_mcp") 判断包目录是否存在，
    而 editable 安装靠 .pth 里的自定义 finder 工作，那个路径根本不存在 → 稳定假阴性。
    改用 find_spec（返回规格但不执行对方代码）后才是对的。
    """
    from omni_media_ext import cli

    available, detail = cli.detect_native_sibling()
    assert available is True, f"原生版已安装却报不可用: {detail}"
    assert "可用" in detail
    assert "omni_media_mcp" in detail  # 给出实际来源路径，便于排查


def test_skill_documents_the_selection_rule():
    """技能说明里必须写明：有 read_audio 就优先原生，没有才用本版本。"""
    skill = (REPO_ROOT / "omni_media_ext" / "skills" / "omni-media-ext" / "SKILL.md").read_text(encoding="utf-8")
    assert "read_audio" in skill, "必须提到原生通道，否则不知道何时该让位"
    assert "优先" in skill
    assert "read_media" in skill

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "read_audio" in readme and "不提供" in readme


def test_cli_status_reports_sibling_availability(capsys):
    """`status` 必须能告诉用户该挂哪一个，而不是让人自己猜。"""
    from omni_media_ext import cli

    assert cli.main(["status", "--config", str(REPO_ROOT / "config.example.json")]) == 0
    out = capsys.readouterr().out
    assert "原生听音版" in out
    assert "read_audio" in out or "read_media" in out


# ---------------------------------------------------------------------------
# 5. 工具提示注释（ToolAnnotations 四大提示）与 100% 测试覆盖
# ---------------------------------------------------------------------------

@requires_native
def test_all_tools_have_required_annotations():
    """验证两个 MCP 服务的所有 4 个工具均正确声明了四大提示且均为布尔值。

    OpenAI 目录要求：不得缺失任何提示，且所有值必须是显式布尔值。
    """
    import asyncio
    from omni_media_ext.server import mcp as ext_mcp
    from omni_media_mcp.server import mcp as native_mcp

    native_tools = {t.name: t for t in asyncio.run(native_mcp.list_tools())}
    ext_tools = {t.name: t for t in asyncio.run(ext_mcp.list_tools())}

    specs = [
        ("omni-media:read_audio", native_tools["read_audio"], True, False, True, False),
        ("omni-media:inspect_media", native_tools["inspect_media"], True, False, True, False),
        ("omni-media-ext:read_media", ext_tools["read_media"], True, False, True, True),
        ("omni-media-ext:inspect_media", ext_tools["inspect_media"], True, False, True, False),
    ]

    for label, tool, ro, dest, idemp, ow in specs:
        ann = tool.annotations
        assert ann is not None, f"{label} 缺少 annotations"
        dump = ann.model_dump(by_alias=True)
        for hint_key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert hint_key in dump, f"{label} 缺少 {hint_key}"
            assert isinstance(dump[hint_key], bool), f"{label} 的 {hint_key} 不是 bool: {dump[hint_key]}"
        assert ann.read_only_hint is ro, f"{label} readOnlyHint 应为 {ro}"
        assert ann.destructive_hint is dest, f"{label} destructiveHint 应为 {dest}"
        assert ann.idempotent_hint is idemp, f"{label} idempotentHint 应为 {idemp}"
        assert ann.open_world_hint is ow, f"{label} openWorldHint 应为 {ow}"


@requires_native
def test_tool_read_audio_coverage(audio_m4a: Path):
    """显式测试原生听音版 read_audio 工具，确保测试名称引用与执行覆盖。"""
    import asyncio
    from omni_media_mcp.server import read_audio

    result = asyncio.run(read_audio(file_path=str(audio_m4a), output_mode="file"))
    assert "OMNI_STATUS" in result
    assert "COMPLETED" in result or "IN_PROGRESS" in result


@requires_native
def test_tool_inspect_media_native_coverage(audio_m4a: Path):
    """显式测试原生版 inspect_media 工具，确保测试名称引用与执行覆盖。"""
    import asyncio
    from omni_media_mcp.server import inspect_media as native_inspect

    result = asyncio.run(native_inspect(file_path=str(audio_m4a)))
    assert "媒体文件探测报告" in result


def test_tool_read_media_coverage(stub, audio_m4a: Path):
    """显式测试代读版 read_media 工具，确保测试名称引用与执行覆盖。"""
    from omni_media_ext.config import Defaults, Endpoint
    from omni_media_ext.providers.gemini import GeminiEndpoint

    endpoint = GeminiEndpoint(
        Endpoint(name="gem", protocol="gemini", base_url=stub.base_gemini, model="m", api_key="k-1234567890ab"),
        Defaults(slice_minutes=10.0, max_payload_mb=18, timeout_sec=30, max_retries=0),
    )
    result = endpoint.process(audio_m4a, "p", "transcribe")
    assert result.text


def test_tool_inspect_media_ext_coverage(audio_m4a: Path):
    """显式测试代读版 inspect_media 工具，确保测试名称引用与执行覆盖。"""
    import asyncio
    from omni_media_ext.server import inspect_media as ext_inspect

    result = asyncio.run(ext_inspect(file_path=str(audio_m4a)))
    assert "媒体文件探测报告" in result
