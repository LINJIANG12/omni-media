"""OmniMedia-Ext 命令行入口（FastCtx 风格）。

子命令：

* ``serve``    —— 启动 MCP stdio 服务（宿主注册的就是这个）
* ``status``   —— 环境/配置/端点/宿主挂载状态诊断（``--probe`` 附带端点可达性）
* ``apply`` / ``unapply`` —— 把服务写入/移出宿主 MCP 配置（注册键 ``omni-media-ext``）
* ``inspect``  —— 本地媒体探测（不联网、不需凭证）
* ``config``   —— 配置文件的初始化 / 定位 / 校验 / 展示（本版本唯一的配置入口）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .adapters.base import build_generic_config
from .adapters.registry import ADAPTER_MAP, get_adapter, get_all_adapters, list_supported_targets
from .config import (
    Config,
    ConfigError,
    candidate_paths,
    default_config_path,
    example_config_text,
    load_config,
    repo_root,
    user_config_path,
    validate_file,
    write_example_config,
)
from .core.inspector import MediaInspector
from .core.limits import PROTOCOL_WHITELIST
from .core.proc import run_quiet
from .providers.base import ProviderRequestError, http_request
from .providers.registry import build_endpoint_from_config
from .server import configure, mcp


def color_text(text: str, color_code: str) -> str:
    """Wraps text in ANSI escape sequence if stdout supports color."""
    if sys.stdout.isatty():
        return f"\033[{color_code}m{text}\033[0m"
    return text


TAG_PASS = color_text("[PASS]", "32")  # Green
TAG_INFO = color_text("[INFO]", "36")  # Cyan
TAG_FAIL = color_text("[FAIL]", "31")  # Red
TAG_WARN = color_text("[WARN]", "33")  # Yellow


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    """Starts MCP stdio transport server."""
    configure(args.config)
    mcp.run(transport="stdio")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def probe_endpoint(name: str, config: Config, timeout_sec: int = 10) -> str:
    """对一个端点做 `GET {base_url}/models` 可达性探测（只在 CLI 里做，不暴露成工具）。"""
    endpoint = config.resolve(name)
    if not endpoint.auth_ready():
        return f"{TAG_WARN} 未配置 api_key，跳过探测"

    headers = dict(endpoint.headers)
    if endpoint.protocol == "gemini" and not any(k.lower() == "x-goog-api-key" for k in headers):
        headers["x-goog-api-key"] = endpoint.api_key
    if endpoint.protocol == "openai" and not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = f"Bearer {endpoint.api_key}"

    try:
        status, raw = http_request(
            f"{endpoint.base_url}/models",
            method="GET",
            headers=headers,
            timeout_sec=min(timeout_sec, 60),
            max_retries=0,
            hint="请检查 base_url 与网络连通性。",
        )
    except ProviderRequestError as exc:
        return f"{TAG_FAIL} 不可达: {str(exc).splitlines()[0]}"
    return f"{TAG_PASS} HTTP {status}，返回 {len(raw)} 字节"


def detect_native_sibling() -> tuple[bool, str]:
    """探测原生听音版（`omni-media-mcp`）是否可用。

    两版是同一个岗位的两种实现，用户需要知道该挂哪一个，因此 `status` 必须直说：
    宿主模型有原生音频模态 → 挂原生版（零凭证、延迟低）；没有 → 挂本版本。

    **刻意不 import 对方**：两版之间「互不 import」是硬契约（两侧 selfcheck 都校验），
    所以这里只做纯元数据 + 文件存在性探测——这恰好也能识别出「editable 安装指向已搬走
    的旧路径」这种坏法（发行版元数据在、包目录却不存在），而不必真的导入它。
    """
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        from importlib.util import find_spec
    except ImportError:  # pragma: no cover
        return False, "无法探测（importlib 不可用）"

    # 可导入性用 find_spec 判定：它返回模块规格但**不执行**对方代码，
    # 因此既回答了「能不能用」，又不违反两版「互不 import」的隔离契约。
    # （不要用 dist.locate_file("omni_media_mcp") 判断：editable 安装靠 .pth 里的
    #   自定义 finder 工作，那个路径算出来根本不存在，会得到假阴性。）
    try:
        spec = find_spec("omni_media_mcp")
    except Exception:  # noqa: BLE001 - find_spec 在父包损坏等情形会抛
        spec = None

    try:
        dist = distribution("omni-media-mcp")
        version = dist.version
    except PackageNotFoundError:
        if spec is None:
            return False, "未安装（缺它就只能用本版本，或 `pip install -e ../mcp`）"
        return False, f"可导入但缺少发行版元数据（{spec.origin}）"

    if spec is None or not spec.origin:
        return False, (
            f"已安装 v{version} 但不可导入——editable 安装多半指向了已搬走的旧路径。"
            f"修法：cd ../mcp && pip install -e ."
        )
    return True, f"可用 v{version}（{spec.origin}）"


def cmd_status(args: argparse.Namespace) -> int:
    """Diagnoses environment, config file, endpoints and host registrations."""
    print("=" * 68)
    print(f"📊 OmniMedia-Ext 运行与配置状态诊断 (v{__version__})")
    print("=" * 68)

    print("\n[ 系统与依赖诊断 ]")
    print(f"  {TAG_PASS} Python: {sys.version.split()[0]} ({sys.executable})")
    try:
        import mcp as mcp_pkg

        print(f"  {TAG_PASS} MCP SDK: {getattr(mcp_pkg, '__version__', 'unknown')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {TAG_FAIL} MCP SDK: 未正确加载 ({exc})")

    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        try:
            res = run_quiet([ffmpeg_bin, "-version"], stdout=subprocess.PIPE, text=True, timeout=5)
            first_line = res.stdout.splitlines()[0] if res.stdout else "unknown version"
            print(f"  {TAG_PASS} FFmpeg: {first_line.split('Copyright')[0].strip()} ({ffmpeg_bin})")
        except Exception:  # noqa: BLE001
            print(f"  {TAG_PASS} FFmpeg: 可用 ({ffmpeg_bin})")
    else:
        print(f"  {TAG_FAIL} FFmpeg: 系统 PATH 中未找到 ffmpeg 可执行文件")

    print(f"  {TAG_PASS} FFprobe: {'可用' if shutil.which('ffprobe') else '未找到（将回退 ffmpeg 探测）'}")
    print(f"  {TAG_INFO} 代码根: {repo_root()}")
    print(f"  {TAG_INFO} 支持的协议: {sorted(PROTOCOL_WHITELIST)}")

    # ---- 配置文件 ----
    print("\n[ 配置文件 ]")
    if args.config:
        print(f"  {TAG_INFO} --config 指定: {Path(args.config).expanduser().resolve()}")
    else:
        print("  " + TAG_INFO + " 候选路径（首个存在者生效）:")
        for i, path in enumerate(candidate_paths(), 1):
            print(f"      {i}. {path}")

    config: Optional[Config] = None
    try:
        config = load_config(args.config)
        print(f"  {TAG_PASS} 生效文件: {config.path}")
    except ConfigError as exc:
        print(f"  {TAG_FAIL} 配置不可用:")
        for line in str(exc).splitlines():
            print(f"      {line}")

    # ---- 端点 ----
    print("\n[ 端点（api_key 已脱敏）]")
    if config is None:
        print(f"  {TAG_WARN} 跳过：配置未就绪")
    else:
        print(f"  {TAG_INFO} active = {config.active or '(未设置，取字典序第一个)'}")
        defaults = config.defaults
        print(
            f"  {TAG_INFO} defaults: slice_minutes={defaults.slice_minutes} "
            f"max_payload_mb={defaults.max_payload_mb} timeout_sec={defaults.timeout_sec} "
            f"max_retries={defaults.max_retries} max_concurrency={defaults.max_concurrency}"
        )
        for name in sorted(config.endpoints):
            endpoint = config.endpoints[name]
            flag = TAG_PASS if endpoint.auth_ready() else TAG_WARN
            mode = f" / {endpoint.openai_mode}" if endpoint.protocol == "openai" else ""
            print(
                f"  {flag} {name}: {endpoint.protocol}{mode} | {endpoint.model} | "
                f"{endpoint.base_url} | key={endpoint.masked_key()}"
            )
            if args.probe:
                print(f"        └─ 探测: {probe_endpoint(name, config)}")

    # ---- 宿主适配器 ----
    print("\n[ 宿主工具箱接入状态 ]")
    for adapter in get_all_adapters():
        status = adapter.check_status()
        tag = TAG_PASS if status["status"] == "PASS" else TAG_INFO
        print(f"  {tag} {adapter.display_name} [{adapter.target_id}]: {status['detail']}")

    # ---- 与原生听音版如何选择 ----
    print("\n[ 与原生听音版（omni-media-mcp）如何选择 ]")
    native_ok, native_detail = detect_native_sibling()
    print(f"  {TAG_PASS if native_ok else TAG_INFO} 原生听音版 omni-media-mcp: {native_detail}")
    print("  " + TAG_INFO + " 选择规则（按宿主模型是否具备音频模态）:")
    print("      • 有原生音频（Gemini / GPT-4o Audio / Codex 等多模态宿主）")
    print("          → 挂 omni-media（read_audio），零凭证、延迟低，本版本可作为兜底同时挂上")
    print("      • 没有原生音频（纯文本宿主）")
    print(f"          → 挂本版本（read_media），由外部模型代读；此时不要挂 omni-media，"
          f"否则 read_audio 只会把音频塞给听不了它的模型")
    print("  " + TAG_INFO + " 两版分页契约同构（同一 OMNI_STATUS 注释 + 同一续读循环），"
          "调用方在两个 MCP 之间可无感切换。")

    print("\n💡 提示: `omni-media-ext apply --target <host>` 接入宿主；"
          "`omni-media-ext config --init` 生成配置模板。")
    print("=" * 68)
    return 0


# ---------------------------------------------------------------------------
# apply / unapply
# ---------------------------------------------------------------------------

def _run_apply_or_unapply(args: argparse.Namespace, apply_mode: bool) -> int:
    target = args.target.lower().strip()
    if target == "all":
        targets = list_supported_targets()
    else:
        if target not in ADAPTER_MAP:
            print(f"{TAG_FAIL} 未知目标: '{target}'。支持的目标: {', '.join(list_supported_targets())}, all")
            return 1
        targets = [target]

    for name in targets:
        adapter = get_adapter(
            name,
            custom_config_path=args.config,
            server_config=getattr(args, "server_config", None),
        )
        # `--skill-dir` 只对 codex 有意义：把打包的 SKILL.md 装到指定目录，
        # 而不是默认的用户级 ~/.agents/skills/。
        skill_dir = getattr(args, "skill_dir", None)
        if skill_dir and hasattr(adapter, "custom_skill_dir"):
            adapter.custom_skill_dir = Path(skill_dir).expanduser().resolve()

        has_change, diff, _ = (
            adapter.preview_apply() if apply_mode else adapter.preview_unapply()
        )

        print("\n" + "-" * 60)
        print(f"目标宿主: {adapter.display_name} ({adapter.get_config_path()})")
        print("-" * 60)

        if not has_change:
            print(
                f"{TAG_PASS} 配置已是最新，无需修改。"
                if apply_mode
                else f"{TAG_INFO} 宿主中未检测到可撤销的 omni-media-ext 配置。"
            )
            continue

        print(("即将写入的配置 Diff 预览:" if apply_mode else "即将移除的配置 Diff 预览:"))
        print(color_text(diff, "33" if apply_mode else "31"))

        if not args.yes:
            verb = "接入" if apply_mode else "移除"
            try:
                answer = input(f"是否确认将 omni-media-ext {verb} {adapter.display_name}? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\n操作已取消。")
                return 1
            if answer not in ("y", "yes"):
                print(f"跳过 {adapter.display_name}。")
                continue

        ok, message = adapter.apply() if apply_mode else adapter.unapply()
        print(f"{TAG_PASS if ok else TAG_FAIL} {message}")

    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    return _run_apply_or_unapply(args, apply_mode=True)


def cmd_unapply(args: argparse.Namespace) -> int:
    return _run_apply_or_unapply(args, apply_mode=False)


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    """Inspects media file metadata (local only, no credentials needed)."""
    try:
        meta = MediaInspector.probe(args.file)
        print(meta.to_markdown())
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"{TAG_FAIL} 探测失败: {exc}", file=sys.stderr)
        return 1


def cmd_print_config(args: argparse.Namespace) -> int:
    """Print a host-neutral MCP stdio configuration and nothing else."""
    print(
        json.dumps(
            build_generic_config(server_config=args.config),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def cmd_config(args: argparse.Namespace) -> int:
    """配置文件的初始化 / 定位 / 校验 / 展示。"""
    acted = False

    if args.init is not None:
        acted = True
        # `--init` 不带值时不能拿空串去 Path()；源码 checkout 写仓库根，
        # site-packages 等非源码安装则写用户级配置目录。
        target = (
            Path(args.init).expanduser().resolve()
            if str(args.init).strip()
            else default_config_path()
        )
        try:
            written = write_example_config(target, force=args.force)
        except ConfigError as exc:
            print(f"{TAG_FAIL} {exc}", file=sys.stderr)
            return 1
        print(f"{TAG_PASS} 已生成配置模板: {written}")
        print(f"{TAG_INFO} 请填入 api_key 后即可使用；该文件已在 .gitignore 中排除。")

    if args.path:
        acted = True
        try:
            config = load_config(args.config)
            print(str(config.path))
        except ConfigError:
            # 尚未创建时，报出「将会使用」的首选路径，方便脚本直接拿去写。
            print(str(candidate_paths(args.config)[0]))
            return 1

    if args.validate:
        acted = True
        path, problems = validate_file(args.config)
        if problems:
            print(f"{TAG_FAIL} 校验未通过:")
            for problem in problems:
                for line in str(problem).splitlines():
                    print(f"      {line}")
            # 配置文件的语义错误（无文件、字段非法）与「尚未填 key」区分开：
            # 后者不算校验失败，因为 inspect_media 与模板期本来就不需要凭证。
            return 1
        print(f"{TAG_PASS} 校验通过: {path}")

    if args.show:
        acted = True
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"{TAG_FAIL} {exc}", file=sys.stderr)
            return 1
        print(json.dumps(config.to_public_dict(), ensure_ascii=False, indent=2))

    if args.example:
        acted = True
        print(example_config_text(), end="")

    if not acted:
        print(f"{TAG_INFO} 配置模板: {repo_root() / 'config.example.json'}")
        print(f"{TAG_INFO} 当前首选配置路径: {default_config_path()}")
        print(f"{TAG_INFO} 用户级默认路径: {user_config_path()}")
        print("用法示例:")
        print("  omni-media-ext config --init          # 在仓库根生成 config.json")
        print("  omni-media-ext config --path          # 打印实际生效的配置文件路径")
        print("  omni-media-ext config --validate      # 校验配置")
        print("  omni-media-ext config --show          # 打印脱敏后的有效配置")
        print("  omni-media-ext config --example       # 打印模板内容到 stdout")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omni-media-ext",
        description=(
            "OmniMedia-Ext：配置文件驱动的外部模型音视频代读 MCP 服务"
            "（Gemini 协议 / OpenAI 协议）"
        ),
    )
    parser.add_argument("--version", action="version", version=f"omni-media-ext {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    config_help = "配置文件路径；缺省按 <repo_root>/config.json → ~/.omni-media-ext/config.json 顺序查找"

    p_serve = subparsers.add_parser("serve", help="启动 MCP stdio 传输服务")
    p_serve.add_argument("--config", "-c", default=None, help=config_help)
    p_serve.set_defaults(func=cmd_serve)

    p_status = subparsers.add_parser("status", help="诊断环境、配置文件、端点与宿主接入状态")
    p_status.add_argument("--config", "-c", default=None, help=config_help)
    p_status.add_argument("--probe", action="store_true", help="对每个端点发 GET /models 探测可达性")
    p_status.set_defaults(func=cmd_status)

    p_apply = subparsers.add_parser("apply", help="接入指定宿主配置 (支持 diff 预览与确认)")
    p_apply.add_argument(
        "--target", "-t", default="antigravity",
        help=f"目标宿主 ({', '.join(list_supported_targets())}, all，默认: antigravity)",
    )
    p_apply.add_argument("--yes", "-y", action="store_true", help="跳过交互确认直接写入")
    p_apply.add_argument("--config", "-c", default=None, help="自定义宿主配置文件路径")
    p_apply.add_argument(
        "--server-config", default=None,
        help="把本服务的配置文件路径写进宿主注册的启动参数（--config <路径>）",
    )
    p_apply.add_argument(
        "--skill-dir", default=None,
        help="仅 codex：把 omni-media-ext 技能装到该目录（默认用户级 ~/.agents/skills/omni-media-ext）",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_unapply = subparsers.add_parser("unapply", help="撤销指定宿主中的 omni-media-ext 接入配置")
    p_unapply.add_argument(
        "--target", "-t", default="antigravity",
        help=f"目标宿主 ({', '.join(list_supported_targets())}, all，默认: antigravity)",
    )
    p_unapply.add_argument("--yes", "-y", action="store_true", help="跳过交互确认直接移除")
    p_unapply.add_argument("--config", "-c", default=None, help="自定义宿主配置文件路径")
    p_unapply.set_defaults(func=cmd_unapply)

    p_inspect = subparsers.add_parser("inspect", help="毫秒级探测音视频元数据与 Token 预估（本地）")
    p_inspect.add_argument("file", help="本地音视频文件路径")
    p_inspect.set_defaults(func=cmd_inspect)

    p_print = subparsers.add_parser(
        "print-config",
        help="输出可粘贴到任意 MCP 宿主的标准 stdio 配置 JSON",
    )
    p_print.add_argument("--config", "-c", default=None, help=config_help)
    p_print.set_defaults(func=cmd_print_config)

    p_config = subparsers.add_parser("config", help="配置文件的初始化 / 定位 / 校验 / 展示")
    p_config.add_argument("--config", "-c", default=None, help=config_help)
    p_config.add_argument(
        "--init", nargs="?", const="", default=None,
        help="生成配置模板（不带值时写到 <repo_root>/config.json，带值则写到指定路径）",
    )
    p_config.add_argument("--force", action="store_true", help="--init 时覆盖已存在的文件")
    p_config.add_argument("--path", action="store_true", help="打印实际生效的配置文件路径")
    p_config.add_argument("--validate", action="store_true", help="校验配置文件的语法与字段")
    p_config.add_argument("--show", action="store_true", help="打印脱敏后的有效配置")
    p_config.add_argument("--example", action="store_true", help="打印配置模板内容")
    p_config.set_defaults(func=cmd_config)

    return parser


def main(args: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    parsed = parser.parse_args(args)
    if not hasattr(parsed, "func"):
        parser.print_help()
        return 0
    return parsed.func(parsed)


if __name__ == "__main__":
    sys.exit(main())
