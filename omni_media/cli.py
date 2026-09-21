"""Command-line interface for OmniMedia."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from .adapters.base import build_generic_config
from .adapters.registry import get_adapter, get_all_adapters, list_supported_targets
from .config import (
    ConfigError,
    default_config_path,
    example_config_path,
    find_config_file,
    load_config,
)
from .core.inspector import MediaInspector
from .server import create_server


def cmd_serve(args: argparse.Namespace) -> int:
    mcp = create_server(mode=args.mode, config_path=args.config)
    mcp.run(transport="stdio")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print("==================================================")
    print("OmniMedia 系统状态与环境诊断")
    print("==================================================")
    # 1. System tools
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    print(f"[*] Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"[*] ffmpeg: {'[PASS] ' + ffmpeg if ffmpeg else '[FAIL] 未找到 (音频切片必需)'}")
    print(f"[*] ffprobe: {'[PASS] ' + ffprobe if ffprobe else '[WARN] 未找到 (将降级为 ffmpeg -i)'}")

    # 2. Config status
    print("\n--- 外部模型配置状态 ---")
    try:
        cfg = load_config(args.config)
        print(f"[PASS] 配置文件: {cfg.path}")
        print(f"[*] 激活端点: {cfg.active}")
        for name, ep in sorted(cfg.endpoints.items()):
            ready = ep.auth_ready()
            state = "READY" if ready else "NO_KEY"
            print(f"    - [{state}] {name} ({ep.protocol} -> {ep.model}) key={ep.masked_key()}")
    except ConfigError as exc:
        print(f"[INFO] {exc}")

    # 3. Host mounting status
    print("\n--- 宿主 Agent 挂载状态 ---")
    server_name = getattr(args, "server_name", "omni-media")
    adapters = get_all_adapters(server_name=server_name)
    for adp in adapters:
        st = adp.check_status()
        print(f"[{st['status']}] {st['display_name']} ({st['target']}): {st['detail']}")

    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    p = Path(args.file).resolve()
    if not p.exists():
        print(f"错误: 文件不存在 `{p}`", file=sys.stderr)
        return 1
    meta = MediaInspector.probe(p)
    if args.json:
        print(json.dumps(meta.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(meta.to_markdown())
    return 0


def cmd_print_config(args: argparse.Namespace) -> int:
    server_name = getattr(args, "server_name", "omni-media")
    cfg = build_generic_config(server_name=server_name, server_module="omni_media.server")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    server_name = getattr(args, "server_name", "omni-media")
    targets = list_supported_targets() if args.target == "all" else [args.target]
    for t in targets:
        try:
            adp = get_adapter(t, custom_config_path=args.config_path, server_name=server_name)
        except ValueError as e:
            # 打错一个宿主名不该看到栈回溯。`get_adapter` 的消息本身已经列出了合法取值。
            print(str(e), file=sys.stderr)
            return 1
        has_change, diff, new_data = adp.preview_apply()
        if not has_change:
            print(f"[*] {adp.display_name}: 配置已是最新，无需修改。")
            continue
        print(f"\n--- {adp.display_name} 配置变更预览 ({adp.get_config_path()}) ---")
        print(diff)
        if not args.yes:
            ans = input(f"是否写入该配置到 {adp.display_name}? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print("操作取消。")
                continue
        adp.write_config(new_data)
        print(f"[PASS] 成功接入 {adp.display_name}")
    return 0


def cmd_unapply(args: argparse.Namespace) -> int:
    server_name = getattr(args, "server_name", "omni-media")
    targets = list_supported_targets() if args.target == "all" else [args.target]
    for t in targets:
        try:
            adp = get_adapter(t, custom_config_path=args.config_path, server_name=server_name)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        success, msg = adp.unapply()
        print(f"[*] {adp.display_name}: {msg}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "locate":
        try:
            print(find_config_file(args.config))
        except ConfigError as e:
            print(f"未定位到有效配置: {e}", file=sys.stderr)
            return 1
    elif args.action == "init":
        target = default_config_path()
        if target.exists() and not args.force:
            print(f"配置文件已存在: {target}（加 --force 可覆盖）")
            return 1
        template_path = example_config_path()
        if not template_path.is_file():
            print(f"模板文件缺失: {template_path}", file=sys.stderr)
            return 1
        if target.exists():
            # 覆盖前先备份：这份配置可能带着真实 `api_key`，而 `--force` 是**就地覆盖**。
            # 实测已造成一次事故（2026-09 覆盖了容器根的活配置，只能靠手工留存的副本恢复），
            # 所以强制留一份可回滚的副本（第二阶段 A11）。备份名已进 .gitignore——
            # 它同样含明文密钥，绝不能进版本库。
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = target.with_name(f"{target.name}.bak.{stamp}")
            shutil.copy2(target, backup)
            print(f"[*] 已备份原配置: {backup}")
        body = template_path.read_text(encoding="utf-8").rstrip("\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8")
        print(f"[PASS] 模板已生成至: {target}")
        print(f"       模板来源: {template_path}")
    elif args.action == "show":
        try:
            cfg = load_config(args.config)
            print(json.dumps(cfg.to_public_dict(), indent=2, ensure_ascii=False))
        except ConfigError as e:
            print(f"配置错误: {e}", file=sys.stderr)
            return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OmniMedia: 音视频感知与听读 MCP 服务")
    parser.set_defaults(server_name="omni-media")
    subparsers = parser.add_subparsers(dest="subcommand", help="子命令")

    # serve
    p_serve = subparsers.add_parser("serve", help="启动 MCP stdio 服务")
    p_serve.add_argument("--mode", choices=["all", "native", "ext"], default="all", help="服务模式")
    p_serve.add_argument("--config", help="配置文件路径")
    p_serve.set_defaults(func=cmd_serve)

    # status
    p_status = subparsers.add_parser("status", help="诊断环境与接入状态")
    p_status.add_argument("--config", help="配置文件路径")
    p_status.set_defaults(func=cmd_status)

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="探测音视频元数据与 Token 预估")
    p_inspect.add_argument("file", help="媒体文件绝对路径")
    p_inspect.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_inspect.set_defaults(func=cmd_inspect)

    # print-config
    p_pc = subparsers.add_parser("print-config", help="打印标准 MCP stdio 配置 JSON")
    p_pc.set_defaults(func=cmd_print_config)

    # apply
    p_apply = subparsers.add_parser("apply", help="接入指定宿主配置")
    p_apply.add_argument("--target", required=True, help=f"宿主目标 ({', '.join(list_supported_targets())}, all)")
    p_apply.add_argument("--config-path", help="自定义宿主配置文件路径")
    p_apply.add_argument("--yes", "-y", action="store_true", help="跳过确认直接写入")
    p_apply.set_defaults(func=cmd_apply)

    # unapply
    p_unapply = subparsers.add_parser("unapply", help="撤销指定宿主配置")
    p_unapply.add_argument("--target", required=True, help=f"宿主目标 ({', '.join(list_supported_targets())}, all)")
    p_unapply.add_argument("--config-path", help="自定义宿主配置文件路径")
    p_unapply.set_defaults(func=cmd_unapply)

    # config
    p_cfg = subparsers.add_parser("config", help="管理外部端点配置")
    p_cfg.add_argument("action", choices=["init", "locate", "show"], help="动作")
    p_cfg.add_argument("--config", help="显式指定配置文件路径")
    p_cfg.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p_cfg.set_defaults(func=cmd_config)

    return parser


def _run(argv: Optional[list[str]], server_name: str) -> int:
    """解析并派发一次 CLI 调用，返回退出码（由 setuptools 生成的启动器 `sys.exit(main())` 消费）。

    两个入口只差「注册名」这一个值：宿主按注册名区分两条听音通道，所以
    `omni-media` 与 `omni-media-ext` 必须报告各自的名字。
    """
    parser = build_parser()
    parser.set_defaults(server_name=server_name)
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


def main(argv: Optional[list[str]] = None) -> int:
    """`omni-media` 入口（注册名 omni-media）。"""
    return _run(argv, "omni-media")


def main_ext(argv: Optional[list[str]] = None) -> int:
    """`omni-media-ext` 入口（注册名 omni-media-ext）。"""
    return _run(argv, "omni-media-ext")


if __name__ == "__main__":
    sys.exit(main())
