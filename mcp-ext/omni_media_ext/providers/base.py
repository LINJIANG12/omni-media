"""外部模型端点的公共基座：HTTP 传输、错误成型、文本归一化、载荷上限。

只依赖标准库（`urllib` / `json` / `base64` / `uuid`），不引入 google-genai、openai、
httpx 等第三方 SDK——这样整个服务的外部依赖只有 `mcp` 与系统 `ffmpeg`。

所有阻塞 HTTP 调用都由调用方通过 `asyncio.to_thread` 派发，避免卡住 MCP 事件循环；
本模块的重试退避用 `time.sleep`，因此**必须**在 worker 线程里执行。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import Defaults, Endpoint
from ..core.limits import ERROR_BODY_EXCERPT_CHARS

# 会被重试的 HTTP 状态码（鉴权/参数类 4xx 绝不重试：重试只会把配额烧在同一个错误上）。
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# 单个 host 的探测/请求统一 UA，便于网关侧排查。
_USER_AGENT = "omni-media-ext/0.1.0"

# 外部模型偶尔**不转录**，而是把它自己的写作计划/自查清单当成结果返回
# （实测：`Self-correction during drafting:` / `Segment 1: 00:05 - 00:12` /
# `Key parts to double check:`）。这类返回非空、HTTP 200，因此能骗过“非空即成功”，
# 被下游当作逐字稿使用。这里给出统一判据，供 provider 在返回前拦一道。
META_TALK_MARKERS = (
    "self-correction",
    "transcribing chunk by chunk",
    "transcribing each section",
    "continue carefully transcribing",
    "let's transcribe",
    "the speaker gives a lecture",
    "the user wants",
    "key parts to double check",
    "double-check all words",
    "key terminology",
    "key terms",
    "ending time:",
    "here is the transcript",
    "the speech transcript",
    "transcription aligns smoothly",
)

# 上游鉴权/配额失败的响应体特征。用于把「网关自己可达、但它连不上上游」
# 与「请求参数/路径配错」区分开——两者的排查方向完全相反。
UPSTREAM_FAILURE_MARKERS = (
    "token acquisition",
    "resource_exhausted",
    "invalid_grant",
    "unauthorized",
    "deadlock",
    "quota",
    "oauth",
    "refresh",
)

# 尾部窗口：元话语返回的最后一段通常还带一句英文收尾（如
# `Transcribing chunk by chunk with proper punctuation, ensuring accuracy.`），
# 而真稿里偶尔残留的英文元话语前缀只出现在**开头**。这条差异是本判据的支点。
_META_TAIL_CHARS = 400

_UPSTREAM_HINT = (
    "这条错误来自端点的**上游**：网关本身可达（`GET /models` 可能是 200），"
    "但它拿不到上游凭证或上游不可达。请先确认本机代理/加速器在运行、且能连上上游，"
    "而不要去改 `/audio/transcriptions` 相关配置——那不是原因。"
)


def count_meta_markers(text: str) -> int:
    """数一数这段文本里出现了几种英文转录指令语。"""
    low = (text or "").strip().lower()
    return sum(1 for marker in META_TALK_MARKERS if marker in low)


def looks_like_model_meta(text: str) -> bool:
    """判断这段返回是不是模型自述的提纲/计划，而不是真实转录内容。

    判据：同一份返回里出现 **>=2 处**英文转录指令语，**或**尾部出现 1 处。

    为什么不用「中文占比低」这类阈值：实测元话语样本里同样夹着大量中文清单
    （P32 那份头部中文占比 68.8%），而正常逐字稿头部中文占比在 62%~84%——
    两者在占比上不可分，用量化阈值只会既漏判又误杀。

    为什么要求 >=2 处：真稿里偶尔残留一句英文元话语前缀（实测 P05 的
    `The speech transcript is as follows:`），但它只出现在开头；元话语返回则头尾
    都带指令语。宁可漏判也不能误杀——把真稿判成元话语会让可用语料被丢弃。

    校准（本判据落地时实测）：3 份真实元话语样本全部命中；同一门课 34 份真稿零误杀。
    """
    probe = (text or "").strip()
    if not probe:
        return False
    if count_meta_markers(probe) >= 2:
        return True
    # 尾部判据只在文本足够长时才生效：否则「尾部窗口」与开头重叠，
    # 真稿开头那一句残留前缀会把它自己顶成尾部命中（实测短文夹具上就是这样误杀的）。
    if len(probe) >= 2 * _META_TAIL_CHARS:
        tail = probe[-_META_TAIL_CHARS:].lower()
        return any(marker in tail for marker in META_TALK_MARKERS)
    return False


def upstream_hint(status: Optional[int], body: str) -> str:
    """上游失败时给出可操作提示；不匹配则返回空串（调用方沿用原有提示）。"""
    if status not in (502, 503, 504):
        return ""
    probe = (body or "").lower()
    if not any(marker in probe for marker in UPSTREAM_FAILURE_MARKERS):
        return ""
    return _UPSTREAM_HINT


class ProviderRequestError(RuntimeError):
    """外部端点请求失败（含 HTTP 状态码与响应体摘要）。"""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ProcessingResult:
    """一次端点调用的结果。"""

    text: str
    endpoint_name: str
    protocol: str
    model: str
    elapsed_sec: float
    finish_reason: str = ""


# ---------------------------------------------------------------------------
# 入参守卫
# ---------------------------------------------------------------------------

def require_media_file(media_path: str | Path) -> Path:
    """解析并校验媒体路径存在且是文件。"""
    target = Path(media_path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"媒体文件不存在: `{target}`")
    if not target.is_file():
        raise ValueError(f"路径不是文件: `{target}`")
    return target


def ensure_payload_size(media_path: str | Path, max_bytes: int) -> int:
    """载荷体积守卫：超过上限时给出可操作的报错，而不是把内存打爆。"""
    size = Path(media_path).stat().st_size
    if max_bytes > 0 and size > max_bytes:
        raise ValueError(
            f"媒体体积 ({size / (1024 * 1024):.2f} MiB) 超过单次请求上限 "
            f"({max_bytes / (1024 * 1024):.0f} MiB)。请调小 duration_minutes 后重试。"
        )
    return size


# ---------------------------------------------------------------------------
# 文本归一化
# ---------------------------------------------------------------------------

def extract_text_content(content: object) -> str:
    """把各家的 `message.content` 归一化成 `str`。

    上游返回可能是 `str` / `None` / 内容块列表（`[{"type":"text","text":...}]`）/
    单个 dict。直接取 `data["choices"][0]["message"]["content"]` 可能拿到 `None` 或
    `list`，在下游炸开；本函数保证返回 `str`，无文本时抛出明确错误。
    """
    if content is None:
        raise ProviderRequestError("模型返回了空的 message.content（无文本输出）。")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            elif isinstance(block, str) and block.strip():
                parts.append(block)
        joined = "\n".join(parts).strip()
        if joined:
            return joined
        raise ProviderRequestError("模型返回了空的内容列表，无法提取文本。")
    if isinstance(content, dict):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value
    raise ProviderRequestError(
        f"无法解析模型返回的 message.content 类型: {type(content).__name__}"
    )


def _excerpt(body: str) -> str:
    body = (body or "").strip()
    if len(body) <= ERROR_BODY_EXCERPT_CHARS:
        return body
    return body[:ERROR_BODY_EXCERPT_CHARS] + f"... (已截断，共 {len(body)} 字符)"


# ---------------------------------------------------------------------------
# HTTP 传输
# ---------------------------------------------------------------------------

def http_request(
    url: str,
    *,
    method: str = "POST",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_sec: int = 300,
    max_retries: int = 2,
    hint: str = "",
) -> Tuple[int, bytes]:
    """带重试的 HTTP 请求，返回 (状态码, 响应体字节)。

    只重试 `_RETRYABLE_STATUS` 与网络类异常，指数退避 `1s, 2s, 4s...`（上限 8s）。
    `hint` 是出错时追加的可操作建议（例如「改用 openai_mode=transcriptions」）。
    """
    request_headers = {"User-Agent": _USER_AGENT}
    if headers:
        request_headers.update({k: v for k, v in headers.items() if v is not None})

    attempts = max(0, int(max_retries)) + 1
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            detail = _excerpt(exc.read().decode("utf-8", errors="replace"))
            last_error = ProviderRequestError(
                f"端点返回 HTTP {exc.code} {exc.reason}\n响应体摘要: {detail}"
                + (f"\n{hint}" if hint else ""),
                status=int(exc.code),
                body=detail,
            )
            if exc.code not in _RETRYABLE_STATUS or attempt == attempts - 1:
                raise last_error from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = ProviderRequestError(
                f"无法访问端点（网络异常/超时）: {reason}" + (f"\n{hint}" if hint else "")
            )
            if attempt == attempts - 1:
                raise last_error from exc
        except OSError as exc:
            last_error = ProviderRequestError(f"请求失败: {exc}" + (f"\n{hint}" if hint else ""))
            if attempt == attempts - 1:
                raise last_error from exc

        time.sleep(min(8.0, 1.0 * (2 ** attempt)))

    # 逻辑上不可达：循环内要么 return 要么 raise。
    raise last_error or ProviderRequestError("请求失败（未知原因）")


def http_post_json(
    url: str,
    payload: Dict[str, Any],
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout_sec: int = 300,
    max_retries: int = 2,
    hint: str = "",
) -> Dict[str, Any]:
    """POST JSON 并解析 JSON 响应。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)

    status, raw = http_request(
        url,
        method="POST",
        headers=merged,
        body=body,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        hint=hint,
    )
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderRequestError(
            f"端点返回了非 JSON 响应（HTTP {status}）: {_excerpt(text)}", status=status
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderRequestError(
            f"端点返回的 JSON 顶层不是对象: {type(parsed).__name__}", status=status
        )
    return parsed


def build_multipart(
    fields: Sequence[Tuple[str, str]],
    files: Sequence[Tuple[str, str, str, bytes]],
) -> Tuple[bytes, str]:
    """手工拼 multipart/form-data（标准库没有构造器）。

    fields: (name, value)；files: (name, filename, content_type, data)。
    """
    boundary = f"----omni-media-ext-{uuid.uuid4().hex}"
    chunks: List[bytes] = []

    for name, value in fields:
        if value is None:
            continue
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    for name, filename, content_type, data in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(data)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# 端点基类
# ---------------------------------------------------------------------------

class BaseEndpoint(ABC):
    """一种线上协议的实现。"""

    protocol: str = "base"

    def __init__(self, endpoint: Endpoint, defaults: Defaults):
        self.endpoint = endpoint
        self.defaults = defaults

    @property
    def name(self) -> str:
        return self.endpoint.name

    @property
    def model(self) -> str:
        return self.endpoint.model

    def _base_headers(self) -> Dict[str, str]:
        """自定义头（可作为鉴权与网关路由的逃生口）。"""
        return dict(self.endpoint.headers)

    @property
    def inflation(self) -> float:
        """请求体相对原始媒体字节的膨胀系数。

        以 base64 内联发送时膨胀约 1.37 倍（4/3 再叠加 JSON 转义）；以 multipart 原样
        上传时为 1.0。切片预算与载荷守卫都用同一个系数折算，避免「切片器算得过、
        守卫拦下来」这种自相矛盾。
        """
        return 1.0

    def payload_budget_bytes(self) -> int:
        """本次请求实际可携带的原始媒体字节上限。"""
        return max(1, int(self.defaults.max_payload_bytes / self.inflation))

    @abstractmethod
    def process(
        self,
        media_path: Path,
        prompt: str,
        mode: str,
        transcript: str = "",
    ) -> ProcessingResult:
        """同步执行一次端点调用。

        Args:
            media_path: 已切片好的本地媒体文件。
            prompt: 提示词（模式模板 + 用户专属指示）。
            mode: transcribe / summarize / qa / custom（协议层用它决定是否需要第二段）。
            transcript: 逐字稿模式的复用通道（第一段已产出的文本，供第二段使用）。

        Note:
            本方法是阻塞的，调用方必须用 `asyncio.to_thread` 派发。
        """
