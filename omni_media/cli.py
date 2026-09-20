"""Command-line interface for OmniMedia."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .adapters.base import build_generic_config
from .adapters.registry import get_adapter, get_all_adapters, list_supported_targets
from .config import ConfigError, default_config_path, find_config_file, load_config
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
        adp = get_adapter(t, custom_config_path=args.config_path, server_name=server_name)
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
        adp = get_adapter(t, custom_config_path=args.config_path, server_name=server_name)
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
        template = {
            "active": "gemini",
            "defaults": {
                "slice_minutes": 10.0,
                "max_concurrency": 3,
            },
            "endpoints": {
                "gemini": {
                    "protocol": "gemini",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                    "model": "gemini-2.5-flash",
                    "api_key": "YOUR_GEMINI_API_KEY",
                },
                "openai": {
                    "protocol": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-audio-preview",
                    "api_key": "YOUR_OPENAI_API_KEY",
                    "openai_mode": "chat",
                }
            }
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[PASS] 模板已生成至: {target}")
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


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))


def main_ext():
    """CLI entrypoint alias for omni-media-ext compatibility."""
    parser = build_parser()
    # default server_name to omni-media-ext
    parser.set_defaults(server_name="omni-media-ext")
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
