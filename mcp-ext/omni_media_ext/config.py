"""配置文件驱动的端点配置层（本版本唯一新增的核心模块）。

设计要点
--------
1. **只用配置文件，不读任何环境变量**。查找顺序（首个存在者生效）::

       --config <路径>                     （CLI 参数；apply 时可写进宿主注册的 args）
       源码 checkout: <repo_root>/config.json
       非源码安装:   ~/.omni-media-ext/config.json

   宿主拉起 MCP 服务时 cwd 是随机的，因此**不能**依赖相对路径；两个默认位置都是
   绝对路径，保证安装形态确定后，任何 cwd 下解析到同一份配置。

2. **JSONC 容忍注释**：允许 `//` 与 `/* */`，方便在配置里写说明。
3. **加载期不校验密钥**：`inspect_media` / `config --validate` 在没有任何密钥时也必须
   可用；只有真正要发请求时（`Endpoint.require_ready()`）才要求凭证就绪。
4. **一切对外输出都脱敏**：`mask_secret()` 是打印密钥的唯一出口。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core.limits import (
    DEFAULT_MAX_PAYLOAD_MB,
    DEFAULT_MAX_RETRIES,
    DEFAULT_SLICE_MINUTES,
    DEFAULT_TIMEOUT_SEC,
    MAX_CONCURRENT_FFMPEG,
    OPENAI_AUDIO_FORMATS,
    OPENAI_MODE_WHITELIST,
    PROTOCOL_WHITELIST,
)

CONFIG_FILENAME = "config.json"
EXAMPLE_FILENAME = "config.example.json"
USER_CONFIG_DIRNAME = ".omni-media-ext"

# 允许作为注释使用的键前缀（`_note` / `_comment` 等），校验时跳过。
_COMMENT_KEY_PREFIX = "_"

# 明确的鉴权头名：配置里出现任一即认为「凭证已由自定义头提供」。
_AUTH_HEADER_NAMES = frozenset({"authorization", "x-goog-api-key", "api-key", "x-api-key"})

# 模板里的占位值：直接复制 config.example.json 时应当被判定为「尚未配置」，
# 否则 `status` 会显示一切就绪，直到真正发请求才以 401 暴露。
_PLACEHOLDERS = frozenset(
    {"replace_me", "your_api_key", "your-api-key", "your_key", "changeme", "todo", "xxx", "sk-xxx"}
)


class ConfigError(ValueError):
    """配置文件缺失、语法错误或语义非法。继承 ValueError 以便调用方统一处理。"""


# ---------------------------------------------------------------------------
# JSONC
# ---------------------------------------------------------------------------

def strip_json_comments(text: str) -> str:
    """去掉 JSONC 的 `//` 与 `/* */` 注释，但保留字符串字面量里的斜杠。

    与 `adapters/base.py` 的同名函数同构：两处各自留一份最小实现，避免配置层
    反向依赖宿主适配层。
    """
    pattern = r'("(?:\\.|[^"\\])*")|(/\*[\s\S]*?\*/|//[^\r\n]*)'

    def _replace(match: re.Match[str]) -> str:
        return match.group(1) if match.group(1) else ""

    return re.sub(pattern, _replace, text)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """仓库根 = `omni_media_ext` 包目录的父目录（由 __file__ 推导，与 cwd 无关）。"""
    return Path(__file__).resolve().parent.parent


def user_config_path() -> Path:
    """用户级配置路径 `~/.omni-media-ext/config.json`。"""
    return Path.home() / USER_CONFIG_DIRNAME / CONFIG_FILENAME


def is_source_checkout() -> bool:
    """Whether this package is running from a source checkout rather than site-packages."""
    root = repo_root()
    return (root / "pyproject.toml").is_file() and (root / "omni_media_ext").is_dir()


def default_config_path() -> Path:
    """Preferred writable config path for the current installation shape."""
    if is_source_checkout():
        return repo_root() / CONFIG_FILENAME
    return user_config_path()


def candidate_paths(explicit: Optional[str | Path] = None) -> List[Path]:
    """返回按优先级排列的候选配置路径（含不存在的，用于报错时全量列出）。"""
    paths: List[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser().resolve())
    preferred = default_config_path()
    paths.append(preferred)
    user_path = user_config_path()
    if user_path != preferred:
        paths.append(user_path)
    return paths


def find_config_file(explicit: Optional[str | Path] = None) -> Path:
    """返回首个存在的配置文件；都不存在则抛 ConfigError 并列出全部候选路径。

    `--config` 由用户显式指定时**必须存在**，否则直接报错（不静默回退到默认位置，
    否则会让人以为改动生效了、实际读的是另一份文件）。
    """
    candidates = candidate_paths(explicit)
    if explicit:
        target = candidates[0]
        if not target.is_file():
            raise ConfigError(
                f"--config 指定的配置文件不存在: `{target}`\n"
                f"请确认路径，或用 `python -m omni_media_ext.cli config --init` 生成模板。"
            )
        return target

    for path in candidates:
        if path.is_file():
            return path

    listed = "\n".join(f"  {i}. {p}" for i, p in enumerate(candidates, 1))
    raise ConfigError(
        "未找到配置文件。按以下顺序查找，均不存在：\n"
        f"{listed}\n"
        "请二选一：\n"
        f"  a) cp config.example.json {candidates[0]}  然后填入 api_key\n"
        "  b) python -m omni_media_ext.cli config --init"
    )


def mask_secret(value: str) -> str:
    """把密钥脱敏成 `abcd...wxyz`；这是密钥唯一允许出现在输出里的形式。"""
    if not value:
        return "(未配置)"
    if len(value) <= 12:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Defaults:
    """请求预算缺省值；工具参数可逐次覆盖。"""

    slice_minutes: float = DEFAULT_SLICE_MINUTES
    max_payload_mb: int = DEFAULT_MAX_PAYLOAD_MB
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    max_retries: int = DEFAULT_MAX_RETRIES
    max_concurrency: int = MAX_CONCURRENT_FFMPEG

    @property
    def max_payload_bytes(self) -> int:
        return int(self.max_payload_mb) * 1024 * 1024


@dataclass
class Endpoint:
    """一个外部模型端点。`protocol` 决定线上形状，其余字段按协议保留。"""

    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    # openai 专属
    openai_mode: str = "chat"
    audio_format: str = "mp3"
    text_model: str = ""
    language: str = ""

    # -- 就绪判定 ---------------------------------------------------------
    def auth_ready(self) -> bool:
        """是否具备发请求的鉴权条件：有真 key，或自定义头里已带鉴权头。

        `REPLACE_ME` 这类模板占位值按「未配置」处理。
        """
        if self.api_key.strip() and self.api_key.strip().lower() not in _PLACEHOLDERS:
            return True
        return any(k.lower() in _AUTH_HEADER_NAMES for k in self.headers)

    def require_ready(self) -> None:
        """发请求前调用；未就绪时给出可操作的报错（而非 401 才暴露）。"""
        if not self.auth_ready():
            raise ConfigError(
                f"端点 `{self.name}` 未配置 api_key（或仍是模板占位值 REPLACE_ME）。\n"
                f"请在配置文件里补上 `endpoints.{self.name}.api_key`。\n"
                f"提示：若你的网关无需密钥，请在 `headers` 里显式给出鉴权头"
                f"（Authorization / x-goog-api-key 等），以满足就绪判定。"
            )

    # -- 对外展示 ---------------------------------------------------------
    def masked_key(self) -> str:
        return mask_secret(self.api_key)

    def to_public_dict(self) -> Dict[str, Any]:
        """脱敏后的端点描述，供 status / media://endpoints / config --show 使用。"""
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
    """一份已校验的配置。"""

    path: Path
    active: str
    defaults: Defaults
    endpoints: Dict[str, Endpoint]

    def resolve(self, name: Optional[str] = None) -> Endpoint:
        """按名字取端点；名字为空时取 `active`，`active` 也为空时取字典序第一个。"""
        if name:
            key = str(name).strip()
            if key not in self.endpoints:
                available = ", ".join(sorted(self.endpoints)) or "(无)"
                raise ConfigError(f"未知端点: `{name}`。可用端点: {available}")
            return self.endpoints[key]

        if self.active:
            if self.active not in self.endpoints:
                available = ", ".join(sorted(self.endpoints)) or "(无)"
                raise ConfigError(
                    f"配置里的 active=`{self.active}` 不在 endpoints 中。可用端点: {available}"
                )
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


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------

def _unknown_keys(data: Dict[str, Any], allowed: set[str]) -> List[str]:
    return [
        k for k in data
        if k not in allowed and not str(k).startswith(_COMMENT_KEY_PREFIX)
    ]


def _validate_defaults(raw: Any, problems: List[str]) -> Defaults:
    defaults = Defaults()
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        problems.append("`defaults` 必须是对象。")
        return defaults

    unknown = _unknown_keys(
        raw,
        {"slice_minutes", "max_payload_mb", "timeout_sec", "max_retries", "max_concurrency"},
    )
    if unknown:
        problems.append(f"`defaults` 存在未知字段: {sorted(unknown)}")

    ranges: Tuple[Tuple[str, float, float], ...] = (
        ("slice_minutes", 0.1, 120.0),
        ("max_payload_mb", 1, 200),
        ("timeout_sec", 10, 3600),
        ("max_retries", 0, 5),
        ("max_concurrency", 1, 8),
    )
    for key, lo, hi in ranges:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"`defaults.{key}` 必须是数字，收到 {type(value).__name__}。")
            continue
        if not (lo <= float(value) <= hi):
            problems.append(f"`defaults.{key}` 应在 [{lo}, {hi}] 区间内，收到 {value}。")
            continue
        setattr(defaults, key, float(value) if key == "slice_minutes" else int(value))
    return defaults


def _validate_endpoint(name: str, raw: Any, problems: List[str]) -> Optional[Endpoint]:
    if not isinstance(raw, dict):
        problems.append(f"`endpoints.{name}` 必须是对象。")
        return None

    allowed = {
        "protocol", "base_url", "api_key", "model", "headers",
        "openai_mode", "audio_format", "text_model", "language",
    }
    unknown = _unknown_keys(raw, allowed)
    if unknown:
        problems.append(
            f"`endpoints.{name}` 存在未知字段 {sorted(unknown)}；"
            f"合法字段为 {sorted(allowed)}（拼错字段名会导致配置静默失效）。"
        )

    protocol = str(raw.get("protocol", "")).strip().lower()
    if protocol not in PROTOCOL_WHITELIST:
        problems.append(
            f"`endpoints.{name}.protocol` 必须是 {sorted(PROTOCOL_WHITELIST)} 之一，"
            f"收到 {raw.get('protocol')!r}。"
        )
        return None

    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    if not base_url:
        problems.append(f"`endpoints.{name}.base_url` 不能为空。")
        return None
    if not re.match(r"^https?://", base_url):
        problems.append(f"`endpoints.{name}.base_url` 必须以 http:// 或 https:// 开头。")
        return None

    model = str(raw.get("model", "")).strip()
    if not model:
        problems.append(f"`endpoints.{name}.model` 不能为空。")
        return None

    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        problems.append(f"`endpoints.{name}.headers` 必须是对象。")
        headers_raw = {}
    headers = {str(k): str(v) for k, v in headers_raw.items()}

    endpoint = Endpoint(
        name=name,
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key=str(raw.get("api_key", "") or ""),
        headers=headers,
    )

    if protocol == "openai":
        mode = str(raw.get("openai_mode", "chat")).strip().lower()
        if mode not in OPENAI_MODE_WHITELIST:
            problems.append(
                f"`endpoints.{name}.openai_mode` 必须是 {sorted(OPENAI_MODE_WHITELIST)} 之一，"
                f"收到 {raw.get('openai_mode')!r}。"
            )
            return None
        endpoint.openai_mode = mode

        fmt = str(raw.get("audio_format", "mp3")).strip().lower()
        if fmt not in OPENAI_AUDIO_FORMATS:
            problems.append(
                f"`endpoints.{name}.audio_format` 必须是 {sorted(OPENAI_AUDIO_FORMATS)} 之一，"
                f"收到 {raw.get('audio_format')!r}。"
            )
            return None
        endpoint.audio_format = fmt
        endpoint.text_model = str(raw.get("text_model", "") or "").strip()
        endpoint.language = str(raw.get("language", "") or "").strip()

    return endpoint


def parse_config(data: Any, path: Path) -> Config:
    """把已解析的 JSON 对象转成 Config；有任何问题就抛 ConfigError（附全部问题）。"""
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 `{path}` 的顶层必须是对象（JSON object）。")

    problems: List[str] = []
    unknown = _unknown_keys(data, {"active", "defaults", "endpoints"})
    if unknown:
        problems.append(f"顶层存在未知字段: {sorted(unknown)}（合法字段: active / defaults / endpoints）")

    defaults = _validate_defaults(data.get("defaults"), problems)

    raw_endpoints = data.get("endpoints")
    endpoints: Dict[str, Endpoint] = {}
    if not isinstance(raw_endpoints, dict) or not raw_endpoints:
        problems.append("`endpoints` 必须是非空对象，且至少包含一个端点。")
    else:
        for name, raw in raw_endpoints.items():
            if str(name).startswith(_COMMENT_KEY_PREFIX):
                continue
            endpoint = _validate_endpoint(str(name), raw, problems)
            if endpoint is not None:
                endpoints[str(name)] = endpoint

    active = str(data.get("active", "") or "").strip()
    if active and active not in endpoints:
        problems.append(
            f"`active`=`{active}` 不在 endpoints 里。可用: {sorted(endpoints) or '(无)'}"
        )

    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"配置文件 `{path}` 校验失败：\n{detail}")

    return Config(path=path, active=active, defaults=defaults, endpoints=endpoints)


def load_config(explicit: Optional[str | Path] = None) -> Config:
    """查找 + 读取 + 校验，一步到位。"""
    path = find_config_file(explicit)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"配置文件无法读取: `{path}` ({exc})") from exc

    cleaned = strip_json_comments(raw_text).strip()
    if not cleaned:
        raise ConfigError(f"配置文件是空的: `{path}`")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件 JSON 语法错误: `{path}` 第 {exc.lineno} 行第 {exc.colno} 列 — {exc.msg}"
        ) from exc

    return parse_config(data, path)


def validate_file(explicit: Optional[str | Path] = None) -> Tuple[Optional[Path], List[str]]:
    """给 `config --validate` 用的软校验：返回 (命中的路径, 问题列表)；不抛异常。"""
    try:
        path = find_config_file(explicit)
    except ConfigError as exc:
        return None, [str(exc)]
    try:
        load_config(explicit)
    except ConfigError as exc:
        return path, [str(exc)]
    return path, []


# ---------------------------------------------------------------------------
# 模板生成
# ---------------------------------------------------------------------------

EXAMPLE_CONFIG: Dict[str, Any] = {
    "_note": (
        "omni-media-ext 配置模板。复制为 config.json 后填入 api_key 即可；"
        "本服务只读配置文件，不读任何环境变量。"
    ),
    "active": "gemini-flash",
    "defaults": {
        "slice_minutes": 10,
        "max_payload_mb": 18,
        "timeout_sec": 300,
        "max_retries": 2,
        "max_concurrency": 3,
    },
    "endpoints": {
        "gemini-flash": {
            "protocol": "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "REPLACE_ME",
            "model": "gemini-2.5-flash",
            "headers": {},
        },
        "openai-audio": {
            "protocol": "openai",
            "openai_mode": "chat",
            "base_url": "https://api.openai.com/v1",
            "api_key": "REPLACE_ME",
            "model": "gpt-4o-audio-preview",
            "audio_format": "mp3",
            "headers": {},
        },
        "whisper-compatible": {
            "protocol": "openai",
            "openai_mode": "transcriptions",
            "base_url": "https://api.openai.com/v1",
            "api_key": "REPLACE_ME",
            "model": "whisper-1",
            "text_model": "gpt-4o-mini",
            "language": "zh",
            "headers": {},
        },
    },
}


def example_config_text() -> str:
    return json.dumps(EXAMPLE_CONFIG, indent=2, ensure_ascii=False) + "\n"


def write_example_config(target: Path, force: bool = False) -> Path:
    """把模板写到 `target`；已存在且未 force 时抛 ConfigError。"""
    target = Path(target).expanduser().resolve()
    if target.exists() and not force:
        raise ConfigError(f"目标已存在，未覆盖: `{target}`（需要覆盖请加 --force）")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(example_config_text(), encoding="utf-8")
    return target
