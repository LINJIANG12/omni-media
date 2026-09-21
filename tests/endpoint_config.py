"""外部端点配置构造器：测试用配置一律指向进程内的 `stub_endpoint`。

为什么必须有这个模块：仓库根的 `config.json` 是**本地实配**（含明文 api_key，
已被 .gitignore 忽略）。任何走到 `read_media` 的调用若没显式传 `config_path`，
`load_config()` 就会落到它、然后真发一次网络请求。所以测试里每一条需要外部端点的
路径都必须拿这里写出来的配置，绝不依赖默认查找顺序。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def stub_endpoints(stub: Any) -> Dict[str, Dict[str, Any]]:
    """一套覆盖三种线上形状的仿真端点：gemini / openai-chat / openai-transcriptions。"""
    return {
        "gem": {
            "protocol": "gemini",
            "base_url": stub.base_gemini,
            "api_key": "stub-gemini-key-1234567890",
            "model": "gemini-stub",
        },
        "oai": {
            "protocol": "openai",
            "openai_mode": "chat",
            "base_url": stub.base_openai,
            "api_key": "stub-openai-key-1234567890",
            "model": "gpt-4o-audio-preview",
        },
        "whisper": {
            "protocol": "openai",
            "openai_mode": "transcriptions",
            "base_url": stub.base_openai,
            "api_key": "stub-whisper-key-1234567890",
            "model": "whisper-1",
            "text_model": "gpt-4o-mini",
            "language": "zh",
        },
    }


def write_ext_config(
    path: Path,
    stub: Any,
    *,
    active: str = "gem",
    slice_minutes: float = 1.0,
    max_payload_mb: int = 18,
    timeout_sec: int = 30,
    max_retries: int = 0,
    endpoints: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Path:
    """写出一份可直接交给 `create_server(config_path=...)` / `--config` 的配置。"""
    data: Dict[str, Any] = {
        "active": active,
        "defaults": {
            "slice_minutes": slice_minutes,
            "max_payload_mb": max_payload_mb,
            "timeout_sec": timeout_sec,
            "max_retries": max_retries,
        },
        "endpoints": stub_endpoints(stub) if endpoints is None else endpoints,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
