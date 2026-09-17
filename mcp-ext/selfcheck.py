#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""omni-media-ext 自检：静态不变量优先，全部离线、无需任何密钥。

与 `mcp/selfcheck.py` 是同一套思路（同为独立仓库、互不 import），但检查的**契约不同**：
本仓库的定位是「配置驱动的外部模型代读」，因此这里重点守三件事——

1. **工具面**：只暴露 `read_media` / `inspect_media`，绝不出现 `read_audio`（原生听音通道）；
2. **配置来源**：一切凭证与端点只来自配置文件，源码里不许读任何 `*_API_KEY` / `*_TOKEN`
   环境变量，也不许靠 `Path.cwd()` 找配置；
3. **互不打扰**：不 import 原版 `omni_media_mcp`、不 import 技能仓库 `src`，
   也不往 `~/.omni-media/` 之类的旧缓存目录写东西。

Run: python selfcheck.py
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omni_media_ext.core.proc import run_quiet  # noqa: E402  （统一抑制 Windows 控制台窗口）

PACKAGE_DIR = REPO_ROOT / "omni_media_ext"
SIBLING_MCP = REPO_ROOT.parent / "mcp"

FAILURES: list[str] = []
SKIPS: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
    except SkipCheck as exc:
        SKIPS.append(f"{name}: {exc}")
        print(f"[SKIP] {name}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"{name}: {exc}")
        print(f"[FAIL] {name}: {exc}")
        return
    suffix = f" — {detail}" if detail else ""
    print(f"[PASS] {name}{suffix}")


class SkipCheck(Exception):
    """用于「环境不具备，无法判定」的情形（不算失败）。"""


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------

def source_files() -> list[Path]:
    return [
        p for p in PACKAGE_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def docstring_nodes(tree: ast.Module) -> set[int]:
    """收集所有文档字符串节点的 id，扫描字符串常量时跳过它们。"""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def string_constants(path: Path) -> list[str]:
    """模块里真正的字符串字面量（不含文档字符串）。"""
    tree = parse(path)
    skip = docstring_nodes(tree)
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


# ---------------------------------------------------------------------------
# 1. 导入与 CLI 入口
# ---------------------------------------------------------------------------

def check_imports_and_cli() -> str:
    import omni_media_ext
    from omni_media_ext import cli, config, prompts, server  # noqa: F401
    from omni_media_ext.providers import registry

    assert omni_media_ext.__version__ == "0.2.0", f"版本号异常: {omni_media_ext.__version__}"
    assert registry.list_protocols() == ["gemini", "openai"], registry.list_protocols()

    # CLI 无参数打印帮助并返回 0
    assert cli.main([]) == 0, "`cli.main([])` 应打印帮助并返回 0"

    # 入口点必须与原版区分开
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "omni-media-ext"' in pyproject, "发行名必须是 omni-media-ext（不能与原版同名）"
    assert "omni-media-ext = " in pyproject and "omni-media-ext-mcp = " in pyproject, "缺少 CLI 入口点"
    assert 'version = "0.2.0"' in pyproject
    assert '"mcp>=2.1.0,<3"' in pyproject
    assert 'name = "omni-media-mcp"' not in pyproject, "不得冒用原版发行名"
    return f"v{omni_media_ext.__version__}，协议 {registry.list_protocols()}"


def check_config_matches_pyproject_version() -> str:
    from omni_media_ext import __version__

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert match, "pyproject.toml 里找不到 version"
    assert match.group(1) == __version__, f"版本号不一致: pyproject={match.group(1)} / __init__={__version__}"
    return match.group(1)


# ---------------------------------------------------------------------------
# 2. 工具面契约
# ---------------------------------------------------------------------------

def check_tool_surface() -> str:
    from omni_media_ext.server import mcp

    tools = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert tools == {"read_media", "inspect_media"}, f"工具面不符: {sorted(tools)}"
    for gone in ("read_audio", "ask_media", "probe_models", "read_media_cloud"):
        assert gone not in tools, f"不应存在工具: {gone}"

    # 源码里也不该留下原生听音通道的**代码痕迹**。
    # 注：只在标识符层面查（注释/文档里说明「本服务不提供 read_audio」是允许且必要的）。
    residual = {"read_audio", "output_mode", "OMNI_MEDIA_OUTPUT_MODE", "ask_media", "probe_models"}
    offenders: list[str] = []
    for path in source_files():
        for node in ast.walk(parse(path)):
            name: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            if name in residual:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, "残留原生听音通道代码: " + "; ".join(offenders)
    return f"{sorted(tools)}"


def check_tools_translate_errors() -> str:
    """两个工具都必须被 `surfaced` 包裹。

    mcp 2.x 只透传 `ToolError` 的文本；漏掉这个装饰器，调用方就看不到
    「端点未配置 api_key」这类可操作信息，只得到一个通用错误名。
    """
    tree = parse(PACKAGE_DIR / "server.py")
    wrapped: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {"read_media", "inspect_media"}:
            decorators = {ast.unparse(d) for d in node.decorator_list}
            assert "surfaced" in decorators, f"{node.name} 缺少 @surfaced 装饰器: {decorators}"
            wrapped.add(node.name)
    assert wrapped == {"read_media", "inspect_media"}, f"未找到工具定义: {wrapped}"
    return "read_media / inspect_media 均带 @surfaced"


def check_tool_annotations() -> str:
    """两个工具都必须配置完整的 ToolAnnotations 提示（四大提示均为布尔值）。"""
    import asyncio
    from omni_media_ext.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert tools.keys() == {"read_media", "inspect_media"}

    expected = {
        "read_media": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
        "inspect_media": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    }
    for name, exp in expected.items():
        ann = tools[name].annotations
        assert ann is not None, f"工具 {name} 缺少 ToolAnnotations"
        assert isinstance(ann.read_only_hint, bool), f"{name}.readOnlyHint 必须是 bool"
        assert isinstance(ann.destructive_hint, bool), f"{name}.destructiveHint 必须是 bool"
        assert isinstance(ann.idempotent_hint, bool), f"{name}.idempotentHint 必须是 bool"
        assert isinstance(ann.open_world_hint, bool), f"{name}.openWorldHint 必须是 bool"
        assert ann.read_only_hint is exp["readOnlyHint"], f"{name}.readOnlyHint 应为 {exp['readOnlyHint']}"
        assert ann.destructive_hint is exp["destructiveHint"], f"{name}.destructiveHint 应为 {exp['destructiveHint']}"
        assert ann.idempotent_hint is exp["idempotentHint"], f"{name}.idempotentHint 应为 {exp['idempotentHint']}"
        assert ann.open_world_hint is exp["openWorldHint"], f"{name}.openWorldHint 应为 {exp['openWorldHint']}"
    return "read_media (openWorld=True) / inspect_media (openWorld=False) 均带全量布尔提示"


# ---------------------------------------------------------------------------
# 3. 配置来源纪律
# ---------------------------------------------------------------------------

_ENV_KEY_PATTERN = re.compile(r"API_KEY|APIKEY|TOKEN|SECRET|OMNI_MEDIA|_CONFIG\b", re.IGNORECASE)


def check_no_credential_env_reads() -> str:
    """源码不得从环境变量取凭证或配置路径（配置只走配置文件）。"""
    offenders: list[str] = []
    for path in source_files():
        tree = parse(path)
        for node in ast.walk(tree):
            # os.environ["X"] / os.environ.get("X")
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                key = node.slice
                value = key.value if isinstance(key, ast.Constant) else ast.unparse(key)
                if isinstance(value, str) and _ENV_KEY_PATTERN.search(value):
                    offenders.append(f"{path.name}: os.environ[{value!r}]")
            if isinstance(node, ast.Call) and ast.unparse(node.func) in ("os.getenv", "os.environ.get"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    if _ENV_KEY_PATTERN.search(node.args[0].value):
                        offenders.append(f"{path.name}: {ast.unparse(node.func)}({node.args[0].value!r})")
    assert not offenders, "源码里出现了凭证/配置环境变量读取: " + "; ".join(offenders)
    return "无 *_API_KEY / *_TOKEN / 配置路径 环境变量读取"


def check_config_lookup_is_cwd_independent() -> str:
    from omni_media_ext import config as cfg

    source = (PACKAGE_DIR / "config.py").read_text(encoding="utf-8")
    assert "Path.cwd()" not in source, "配置查找不得依赖当前工作目录"

    candidates = cfg.candidate_paths()
    assert candidates, "候选配置路径为空"
    for path in candidates:
        assert path.is_absolute(), f"候选路径必须是绝对路径: {path}"

    assert cfg.repo_root().is_absolute() and cfg.user_config_path().is_absolute()
    names = [p.name for p in candidates]
    assert all(name == cfg.CONFIG_FILENAME for name in names), names
    return f"{len(candidates)} 个绝对候选路径"


def check_config_example_matches_template() -> str:
    from omni_media_ext import config as cfg

    shipped = REPO_ROOT / cfg.EXAMPLE_FILENAME
    assert shipped.is_file(), f"缺少 {cfg.EXAMPLE_FILENAME}"
    assert shipped.read_text(encoding="utf-8") == cfg.example_config_text(), (
        f"{cfg.EXAMPLE_FILENAME} 与 config.py 里的模板不一致（两处会漂移）"
    )

    config_json = REPO_ROOT / cfg.CONFIG_FILENAME
    if config_json.exists():
        # 本地实配也必须合法（否则用户以为改好了，其实一调用就报错）
        cfg.load_config(config_json)
    return f"{cfg.EXAMPLE_FILENAME} 与代码模板一致"


def check_no_home_cache_writes() -> str:
    """不得再往 `~/.omni-media/`（原版切片缓存）写东西。

    允许 `.omni-media-ext`（那是本版本自己的用户级配置目录名）。
    """
    pattern = re.compile(r"\.omni-media(?![-\w])")
    offenders: list[str] = []
    for path in source_files():
        for text in string_constants(path):
            if pattern.search(text):
                offenders.append(f"{path.name}: {text!r}")
    assert not offenders, "残留旧版缓存目录引用: " + "; ".join(offenders)
    return "无 ~/.omni-media 缓存引用"


# ---------------------------------------------------------------------------
# 4. 互不打扰
# ---------------------------------------------------------------------------

def check_no_sibling_imports() -> str:
    """不 import 原版 MCP / 技能仓库。

    只禁**导入**，不禁文本提及：`status` 要按发行版名探测原版是否可用（`status` 里那行
    `locate_file("omni_media_mcp")` 是只读元数据查询，不是依赖）。文本层面仍拦下
    `import omni_media_mcp` / `from omni_media_mcp` 这类写法，防动态绕过。
    """
    textual_import = re.compile(r"^\s*(?:import|from)\s+(?:omni_media_mcp|src)\b", re.MULTILINE)
    for path in source_files():
        modules = imported_modules(path)
        assert "omni_media_mcp" not in modules, f"{path.name} import 了原版 omni_media_mcp"
        assert "src" not in modules, f"{path.name} import 了技能仓库 src"
        assert not textual_import.search(path.read_text(encoding="utf-8")), \
            f"{path.name} 文本里出现原版/技能仓库的 import 语句"
    return "不 import omni_media_mcp / 技能仓库 src（探测仅用元数据）"


def check_native_compat_contract() -> str:
    """与原生听音版（omni-media-mcp）的兼容契约。

    两版是同一岗位的两种实现，靠「同名同语义的分页状态注释 + 同名同型的切片参数」
    让调用方无感切换。这里守住契约里最容易在重构中被改坏的三处。
    """
    from omni_media_ext.server import (
        STATUS_CONTRACT_VERSION,
        STATUS_SHARED_CONTINUATION_KEYS,
        STATUS_SHARED_KEYS,
        STATUS_TAG,
    )

    assert STATUS_TAG == "OMNI_STATUS", f"分页标签必须与原生版同名，当前: {STATUS_TAG}"
    assert STATUS_CONTRACT_VERSION == 1, STATUS_CONTRACT_VERSION
    assert STATUS_SHARED_KEYS == (
        "contract_version", "status", "mode", "is_finished", "start_time", "end_time", "total_duration",
    ), STATUS_SHARED_KEYS
    assert set(STATUS_SHARED_CONTINUATION_KEYS) == {"next_start_time", "next_duration_minutes"}

    source = (PACKAGE_DIR / "server.py").read_text(encoding="utf-8")
    assert '"contract_version": STATUS_CONTRACT_VERSION' in source
    # `mode` 在两版里都必须是切片模式语义；任务预设只能放 `task`（同名不同义会被静默误读）
    assert '"mode": "chunked" if is_sliced else "oneshot"' in source, "mode 必须是切片模式"
    assert '"task": mode_key' in source, "任务预设必须写进 task"
    assert '"mode": mode_key' not in source, "任务预设不能占用共有键 mode"

    # 切片参数必须与原生版同名
    from omni_media_ext.server import mcp

    schema = next(t.parameters for t in mcp._tool_manager.list_tools() if t.name == "read_media")
    assert {"file_path", "start_time", "duration_minutes"} <= set(schema.get("properties", {})), schema
    assert schema.get("required") == ["file_path"], schema.get("required")

    # 选择规则必须被文档化（否则用户不知道该挂哪一个）
    skill = (PACKAGE_DIR / "skills" / "omni-media-ext" / "SKILL.md").read_text(encoding="utf-8")
    assert "read_audio" in skill and "优先" in skill, "SKILL.md 缺少「有原生听音就让位」的规则"
    return f"{STATUS_TAG} 同名同语义，切片参数同型，让位规则已文档化"


def check_sibling_repo_untouched() -> str:
    """反向隔离：原版 MCP 仓库里不应出现本版本的任何标识。"""
    if not SIBLING_MCP.is_dir():
        raise SkipCheck(f"未找到同级原版仓库: {SIBLING_MCP}")
    hits: list[str] = []
    for path in SIBLING_MCP.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "omni_media_ext" in text or "omni-media-ext" in text:
            hits.append(str(path.relative_to(SIBLING_MCP)))
    assert not hits, "原版仓库被写入了本版本的标识: " + ", ".join(hits)
    return "原版 mcp/ 无本版本标识"


# ---------------------------------------------------------------------------
# 5. 适配器契约
# ---------------------------------------------------------------------------

def check_adapters() -> str:
    from omni_media_ext.adapters.registry import (
        get_adapter,
        get_all_adapters,
        list_supported_targets,
    )

    expected = {"opencode", "zcode", "dsh", "codex", "antigravity"}
    assert set(list_supported_targets()) == expected, list_supported_targets()

    for adapter in get_all_adapters():
        entry = adapter.build_entry()

        # 只允许注入 PYTHONPATH：既不带凭证，也不带任何本版本已不存在的旋钮
        # （原版 codex 适配器曾多注入一个 OMNI_MEDIA_OUTPUT_MODE）。
        env = entry.get("env") or {}
        assert set(env) == {"PYTHONPATH"}, (
            f"{adapter.target_id} 适配器注入了多余的环境变量: {sorted(env)}"
        )
        assert env["PYTHONPATH"], f"{adapter.target_id} 适配器的 PYTHONPATH 为空"

        # 启动命令必须是本包的 server 模块
        assert entry["args"][:2] == ["-m", "omni_media_ext.server"], entry["args"]

        # 注册键必须是 omni-media-ext（与原版 omni-media 互不覆盖）
        applied = adapter.build_applied_config({})
        flat = json.dumps(applied, ensure_ascii=False)
        assert "omni-media-ext" in flat, f"{adapter.target_id} 未注册为 omni-media-ext"

        # 每个适配器都必须能接受这两个关键字参数。
        # （Codex 自己覆写 __init__ 时曾漏掉 server_config，导致 apply --server-config 直接 TypeError。）
        probe = get_adapter(adapter.target_id, custom_config_path=None, server_config=None)
        assert probe.server_config is None, f"{adapter.target_id} 未透传 server_config"

        # --server-config 时必须把 --config 写进启动参数
        configured = get_adapter(adapter.target_id, server_config="X:/tmp/cfg.json")
        args = configured.build_entry()["args"]
        assert args == [
            "-m", "omni_media_ext.server", "--config", str(Path("X:/tmp/cfg.json")),
        ], f"{adapter.target_id} 的 server-config 注入异常: {args}"

    return f"{sorted(expected)} 五个适配器齐备，注册键 omni-media-ext，server-config 注入正常"


# ---------------------------------------------------------------------------
# 6. 代码卫生
# ---------------------------------------------------------------------------

_HARDCODED_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/(?:Users|home)/)")
_PROC_LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}


def check_no_hardcoded_paths() -> str:
    offenders: list[str] = []
    for path in source_files():
        for text in string_constants(path):
            if _HARDCODED_PATH.match(text):
                offenders.append(f"{path.name}: {text!r}")
    assert not offenders, "硬编码本机绝对路径: " + "; ".join(offenders)
    return "无本机绝对路径"


def check_subprocess_goes_through_run_quiet() -> str:
    """所有子进程启动都必须走 core/proc.py 的 run_quiet（Windows 不闪黑窗）。"""
    offenders: list[str] = []
    for path in source_files():
        if path.name == "proc.py":
            continue  # run_quiet 本身就实现在这里
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "subprocess" and node.attr in _PROC_LAUNCHERS:
                offenders.append(f"{path.name}: subprocess.{node.attr}")
    assert not offenders, "存在绕过 run_quiet 的子进程调用: " + "; ".join(offenders)

    from omni_media_ext.core import proc as proc_mod

    assert proc_mod.CREATE_NO_WINDOW, "CREATE_NO_WINDOW 不可用"
    assert proc_mod.quiet_kwargs().get("creationflags") == proc_mod.CREATE_NO_WINDOW
    return "统一走 run_quiet"


def check_server_module_help() -> str:
    """`python -m omni_media_ext.server --help` 必须能正常退出（不启动 stdio）。"""
    result = run_quiet(
        [sys.executable, "-m", "omni_media_ext.server", "--help"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"退出码 {result.returncode}: {result.stderr[:300]}"
    assert "--config" in (result.stdout or ""), "server --help 里应列出 --config"
    return "server 入口可用"


# ---------------------------------------------------------------------------
# 7. 文档一致性
# ---------------------------------------------------------------------------

def check_readme_mentions_contracts() -> str:
    readme = REPO_ROOT / "README.md"
    assert readme.is_file(), "缺少 README.md"
    text = readme.read_text(encoding="utf-8")
    for needle in ("config.json", "gemini", "openai", "read_media", "inspect_media", "pip install"):
        assert needle in text, f"README 缺少关键说明: {needle}"
    assert "read_audio" in text, "README 应说明与原生听音版的关系（read_audio 不提供）"

    skill = PACKAGE_DIR / "skills" / "omni-media-ext" / "SKILL.md"
    assert skill.is_file(), f"缺少技能说明: {skill}"
    assert "read_media" in skill.read_text(encoding="utf-8")
    return "README + SKILL.md 齐备"


def main() -> int:
    print("=" * 68)
    print("omni-media-ext 自检（外部模型代读版，离线、零密钥）")
    print("=" * 68)

    check("模块导入与 CLI 入口", check_imports_and_cli)
    check("版本号一致（pyproject / __init__）", check_config_matches_pyproject_version)
    check("工具面契约（read_media / inspect_media）", check_tool_surface)
    check("工具提示注解（ToolAnnotations 四大提示）", check_tool_annotations)
    check("工具错误可透传（@surfaced）", check_tools_translate_errors)
    check("配置只来自文件（无凭证环境变量）", check_no_credential_env_reads)
    check("配置查找与 cwd 无关", check_config_lookup_is_cwd_independent)
    check("配置模板与代码一致", check_config_example_matches_template)
    check("不写旧版缓存目录（~/.omni-media）", check_no_home_cache_writes)
    check("不 import 原版 MCP / 技能仓库", check_no_sibling_imports)
    check("与原生听音版的兼容契约", check_native_compat_contract)
    check("原版 mcp/ 仓库未被写入本版本标识", check_sibling_repo_untouched)
    check("五个宿主适配器（零凭证 + 注册键）", check_adapters)
    check("源码无硬编码本机路径", check_no_hardcoded_paths)
    check("子进程统一走 run_quiet", check_subprocess_goes_through_run_quiet)
    check("server 入口 --help 可用", check_server_module_help)
    check("文档一致性（README / SKILL.md）", check_readme_mentions_contracts)

    print("=" * 68)
    if FAILURES:
        print(f"[FAILED] {len(FAILURES)} 项未通过:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    if SKIPS:
        print(f"[OK] 全部自检通过（{len(SKIPS)} 项因环境跳过）")
    else:
        print("[OK] 全部自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
