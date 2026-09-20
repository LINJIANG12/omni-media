"""Configuration management for OmniMedia external endpoints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core.limits import MAX_CONCURRENT_FFMPEG

CONFIG_FILENAME = "config.json"
EXAMPLE_FILENAME = "config.example.json"
USER_CONFIG_DIRNAME = ".omni-media"
LEGACY_USER_CONFIG_DIRNAME = ".omni-media-ext"

_COMMENT_KEY_PREFIX = "_"
_AUTH_HEADER_NAMES = frozenset({"authorization", "x-goog-api-key", "api-key", "x-api-key"})
_PLACEHOLDERS = frozenset(
    {"replace_me", "your_api_key", "your-api-key", "your_key", "changeme", "todo", "xxx", "sk-xxx"}
)


class ConfigError(ValueError):
    """Configuration missing or invalid."""


def strip_json_comments(text: str) -> str:
    pattern = r'("(?:\\.|[^"\\])*")|(/\*[\s\S]*?\*/|//[^\r\n]*)'

    def _replace(match: re.Match[str]) -> str:
        return match.group(1) if match.group(1) else ""

    return re.sub(pattern, _replace, text)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def user_config_path() -> Path:
    p = Path.home() / USER_CONFIG_DIRNAME / CONFIG_FILENAME
    if not p.exists():
        legacy = Path.home() / LEGACY_USER_CONFIG_DIRNAME / CONFIG_FILENAME
        if legacy.exists():
            return legacy
    return p


def is_source_checkout() -> bool:
    root = repo_root()
    return (root / "pyproject.toml").is_file() and (root / "omni_media").is_dir()


def default_config_path() -> Path:
    if is_source_checkout():
        return repo_root() / CONFIG_FILENAME
    return user_config_path()


def candidate_paths(explicit: Optional[str | Path] = None) -> List[Path]:
    paths: List[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser().resolve())
    preferred = default_config_path()
    paths.append(preferred)
    u_path = user_config_path()
    if u_path != preferred and u_path not in paths:
        paths.append(u_path)
    legacy = Path.home() / LEGACY_USER_CONFIG_DIRNAME / CONFIG_FILENAME
    if legacy not in paths:
        paths.append(legacy)
    return paths


def find_config_file(explicit: Optional[str | Path] = None) -> Path:
    candidates = candidate_paths(explicit)
    if explicit:
        target = candidates[0]
        if not target.is_file():
            raise ConfigError(f"--config 指定的配置文件不存在: `{target}`")
        return target

    for path in candidates:
        if path.is_file():
            return path

    listed = "\n".join(f"  {i}. {p}" for i, p in enumerate(candidates, 1))
    raise ConfigError(
        f"未找到配置文件。按以下顺序查找，均不存在：\n{listed}\n"
        f"请运行 `omni-media config --init` 或配置 config.json"
    )


def mask_secret(value: str) -> str:
    if not value:
        return "(未配置)"
    if len(value) <= 12:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


@dataclass
class Defaults:
    slice_minutes: float = 10.0
    max_payload_mb: int = 20
    timeout_sec: int = 120
    max_retries: int = 2
    max_concurrency: int = MAX_CONCURRENT_FFMPEG

    @property
    def max_payload_bytes(self) -> int:
        return int(self.max_payload_mb) * 1024 * 1024


@dataclass
class Endpoint:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    openai_mode: str = "chat"
    audio_format: str = "mp3"
    text_model: str = ""
    language: str = ""

    def auth_ready(self) -> bool:
        if self.api_key.strip() and self.api_key.strip().lower() not in _PLACEHOLDERS:
            return True
        return any(k.lower() in _AUTH_HEADER_NAMES for k in self.headers)

    def require_ready(self) -> None:
        if not self.auth_ready():
            raise ConfigError(
                f"端点 `{self.name}` 未配置 api_key（或仍是模板占位值）。\n"
                f"请在配置文件里补上 `endpoints.{self.name}.api_key`。"
            )

    def masked_key(self) -> str:
        return mask_secret(self.api_key)

    def to_public_dict(self) -> Dict[str, Any]:
        public: Dict[str, Any] = {
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "api_key": self.masked_key(),
            "auth_ready": self.auth_ready(),
        }
        if self.protocol == "openai":
            public["openai_mode"] = self.openai_mode
            if self.openai_mode == "chat":
                public["audio_format"] = self.audio_format
            else:
                public["text_model"] = self.text_model or self.model
                if self.language:
                    public["language"] = self.language
        if self.headers:
            public["headers"] = sorted(self.headers.keys())
        return public


@dataclass
class Config:
    path: Path
    active: str
    defaults: Defaults
    endpoints: Dict[str, Endpoint]

    def resolve(self, name: Optional[str] = None) -> Endpoint:
        if name:
            key = str(name).strip()
            if key not in self.endpoints:
                available = ", ".join(sorted(self.endpoints)) or "(无)"
                raise ConfigError(f"未知端点: `{name}`。可用端点: {available}")
            return self.endpoints[key]

        if self.active:
            if self.active not in self.endpoints:
                available = ", ".join(sorted(self.endpoints)) or "(无)"
                raise ConfigError(f"配置里的 active=`{self.active}` 不在 endpoints 中。可用端点: {available}")
            return self.endpoints[self.active]

        if not self.endpoints:
            raise ConfigError(f"配置文件 `{self.path}` 里没有任何 endpoints。")
        return self.endpoints[sorted(self.endpoints)[0]]

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "config_file": str(self.path),
            "active": self.active or "(未设置，取字典序第一个)",
            "defaults": {
                "slice_minutes": self.defaults.slice_minutes,
                "max_payload_mb": self.defaults.max_payload_mb,
                "timeout_sec": self.defaults.timeout_sec,
                "max_retries": self.defaults.max_retries,
                "max_concurrency": self.defaults.max_concurrency,
            },
            "endpoints": {n: e.to_public_dict() for n, e in sorted(self.endpoints.items())},
        }


def load_config(explicit: Optional[str | Path] = None) -> Config:
    path = find_config_file(explicit)
    text = path.read_text(encoding="utf-8")
    clean_text = strip_json_comments(text)
    try:
        data = json.loads(clean_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 `{path}` JSON 解析失败: {exc}")

    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 `{path}` 顶层必须是 JSON 对象。")

    defaults = Defaults()
    if "defaults" in data:
        raw_d = data["defaults"]
        if isinstance(raw_d, dict):
            for k in ("slice_minutes", "max_payload_mb", "timeout_sec", "max_retries", "max_concurrency"):
                if k in raw_d:
                    setattr(defaults, k, float(raw_d[k]) if k == "slice_minutes" else int(raw_d[k]))

    endpoints: Dict[str, Endpoint] = {}
    raw_eps = data.get("endpoints", {})
    if isinstance(raw_eps, dict):
        for name, ep_data in raw_eps.items():
            if str(name).startswith(_COMMENT_KEY_PREFIX):
                continue
            if isinstance(ep_data, dict):
                ep = Endpoint(
                    name=name,
                    protocol=str(ep_data.get("protocol", "gemini")).lower(),
                    base_url=str(ep_data.get("base_url", "")),
                    model=str(ep_data.get("model", "")),
                    api_key=str(ep_data.get("api_key", "")),
                    headers=dict(ep_data.get("headers", {})),
                    openai_mode=str(ep_data.get("openai_mode", "chat")),
                    audio_format=str(ep_data.get("audio_format", "mp3")),
                    text_model=str(ep_data.get("text_model", "")),
                    language=str(ep_data.get("language", "")),
                )
                endpoints[name] = ep

    active = str(data.get("active", "")).strip()
    return Config(path=path, active=active, defaults=defaults, endpoints=endpoints)
