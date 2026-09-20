"""OpenAI 协议端点，覆盖两种线上形状（由配置 `openai_mode` 选择）：

``chat``（默认）
    单段：`POST {base_url}/chat/completions`，content 里同时放文本与
    ``{"type":"input_audio","input_audio":{"data":<b64>,"format":"mp3"|"wav"}}``。
    模型既听音又推理，因此 transcribe / summarize / qa / custom 四个 mode 都直接用一段请求完成。

``transcriptions``
    `POST {base_url}/audio/transcriptions`（multipart），任何 Whisper 兼容端点都能用，
    但它本身只产出逐字稿。于是：``mode == "transcribe"`` 时一段结束；其余 mode 自动追加
    **第二段纯文本** `chat/completions`，把逐字稿交给 `text_model` 做总结/问答。

为什么 chat 模式必须真转码：`input_audio` 只接受 mp3/wav，而本服务的切片是 m4a(AAC)。
把 m4a 字节原样声明成 `format="mp3"` 会得到一个损坏的载荷，因此走
`MediaPreprocessor.transcode_to_mp3`。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..config import ConfigError
from ..core.preprocessor import MediaPreprocessor
from ..core.temp_manager import ManagedTempDir
from ..prompts import TRANSCRIPT_WRAPPER
from .base import (
    BaseEndpoint,
    ProcessingResult,
    ProviderRequestError,
    build_multipart,
    ensure_payload_size,
    extract_text_content,
    http_post_json,
    http_request,
    looks_like_model_meta,
    require_media_file,
    upstream_hint,
)

# chat 模式里 input_audio 只认这两种容器。
_TRANSCODE_TARGETS = ("mp3", "wav")

_INPUT_AUDIO_HINT = (
    "若该端点不支持 `input_audio`，请把配置里的 `openai_mode` 改成 "
    "`transcriptions`（走 /audio/transcriptions，兼容 Whisper 系端点）。"
)


def _format_verbose_segments(segments: Any) -> str:
    """若端点返回了 segments，按 `[HH:MM:SS]` 渲染，便于满足转录的「时间线」要求。"""
    if not isinstance(segments, list):
        return ""
    lines: List[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        start = seg.get("start")
        try:
            start_f = float(start)
        except (TypeError, ValueError):
            lines.append(text)
            continue
        lines.append(f"[{MediaPreprocessor.format_time_str(start_f)}] {text}")
    return "\n".join(lines).strip()


class OpenAIEndpoint(BaseEndpoint):
    """OpenAI 兼容协议（chat/completions 与 audio/transcriptions）。"""

    protocol = "openai"

    @property
    def inflation(self) -> float:
        """chat 模式 base64 内联（≈1.37 倍）；transcriptions 模式 multipart 原样上传。"""
        return 1.0 if self.endpoint.openai_mode == "transcriptions" else 1.4

    # -- 公共 -------------------------------------------------------------
    def _headers(self, *, json_body: bool) -> Dict[str, str]:
        headers = self._base_headers()
        if json_body:
            headers.setdefault("Content-Type", "application/json")
        if self.endpoint.api_key and not any(
            k.lower() == "authorization" for k in headers
        ):
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        return headers

    def _text_model(self) -> str:
        """第二段（纯文本）用的模型名。"""
        model = self.endpoint.text_model or ""
        if not model:
            raise ConfigError(
                f"端点 `{self.name}` 的 openai_mode=transcriptions，且本次 mode 需要第二段文本处理，"
                f"但配置里没有 `text_model`。\n"
                f"请在 `endpoints.{self.name}.text_model` 填一个对话模型"
                f"（如 gpt-4o-mini）。若只想出逐字稿，请改用 mode=transcribe。"
            )
        return model

    # -- chat 模式 --------------------------------------------------------
    def build_chat_payload(self, media_b64: str, audio_format: str, prompt: str) -> Dict[str, Any]:
        """构造 `input_audio` 请求体（独立成方法，便于单测断言精确形状）。"""
        payload: Dict[str, Any] = {
            "model": self.endpoint.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": media_b64, "format": audio_format},
                        },
                    ],
                }
            ],
        }
        # `modalities` 只有 OpenAI 自家的 audio 模型认；对通用兼容端点发出去可能被 400 拒掉。
        if "audio" in self.endpoint.model.lower():
            payload["modalities"] = ["text"]
        return payload

    def _encode_audio(self, target: Path, tmp_dir: Path) -> Tuple[str, str]:
        """把媒体准备成 input_audio 接受的容器，返回 (base64, format)。"""
        wanted = self.endpoint.audio_format
        suffix = target.suffix.lower().lstrip(".")
        if suffix == wanted:
            return base64.b64encode(target.read_bytes()).decode("ascii"), wanted
        if suffix not in _TRANSCODE_TARGETS and wanted != "mp3":
            # 只有 mp3 有现成转码器；wav 目标且源非 wav 时明确报错而不是发假载荷。
            raise ConfigError(
                f"端点 `{self.name}` 的 audio_format={wanted}，但源切片是 .{suffix}，"
                f"本版本只实现了转 mp3。请把 audio_format 改成 mp3。"
            )

        out = tmp_dir / f"{target.stem}_input_audio.mp3"
        MediaPreprocessor.transcode_to_mp3(target, out)
        if not out.exists() or out.stat().st_size == 0:
            raise ProviderRequestError(f"音频转码失败：未生成 {out.name}")
        return base64.b64encode(out.read_bytes()).decode("ascii"), "mp3"

    def _process_chat(
        self, target: Path, prompt: str, tmp_dir: Path
    ) -> Tuple[str, str]:
        ensure_payload_size(target, self.payload_budget_bytes())
        media_b64, audio_format = self._encode_audio(target, tmp_dir)
        data = http_post_json(
            f"{self.endpoint.base_url}/chat/completions",
            self.build_chat_payload(media_b64, audio_format, prompt),
            headers=self._headers(json_body=True),
            timeout_sec=self.defaults.timeout_sec,
            max_retries=self.defaults.max_retries,
            hint=_INPUT_AUDIO_HINT,
        )
        choices = data.get("choices") or []
        if not choices:
            raise ProviderRequestError(
                f"端点未返回任何 choices。原始响应摘要: {str(data)[:400]}"
            )
        message = (choices[0] or {}).get("message") or {}
        text = extract_text_content(message.get("content"))
        return text, str((choices[0] or {}).get("finish_reason", "") or "")

    # -- transcriptions 模式 ----------------------------------------------
    _ASR_HINT = (
        "请确认该端点确实提供 OpenAI 兼容的 /audio/transcriptions，"
        "且 `model` 是转录模型（如 whisper-1）。"
    )

    # 转录返回格式，按顺序尝试：`verbose_json` 会连同 segments 一起返回，我们据此渲染出
    # 行首 `[HH:MM:SS]`（引擎的真实位置，不是模型猜的）；端点不认这个参数（4xx）时退回
    # `json`（只有一整块纯文本），格式差异不该让整门课停在这里。
    _ASR_FORMATS = ("verbose_json", "json")

    def build_transcription_form(
        self, target: Path, response_format: str = "verbose_json"
    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str, str, bytes]], str]:
        """构造 multipart 字段/文件与 mime（独立成方法，便于单测断言）。"""
        mime, _ = mimetypes.guess_type(target.name)
        if not mime:
            mime = "audio/mp4" if target.suffix.lower() in (".m4a", ".mp4") else "application/octet-stream"

        fields: List[Tuple[str, str]] = [
            ("model", self.endpoint.model),
            ("response_format", response_format),
        ]
        if self.endpoint.language:
            fields.append(("language", self.endpoint.language))

        files = [("file", target.name, mime, target.read_bytes())]
        return fields, files, mime

    @staticmethod
    def _extract_transcript(status: int, raw: bytes) -> str:
        """把 `/audio/transcriptions` 的响应体解析成逐字稿文本。

        解析不了或内容为空时抛 `ProviderRequestError`——与拆分前的行为逐字一致。
        """
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text.strip().startswith("{") else {"text": text}
        except json.JSONDecodeError:
            parsed = {"text": text}

        if isinstance(parsed, dict):
            rendered = _format_verbose_segments(parsed.get("segments"))
            if rendered:
                return rendered
            body_text = parsed.get("text")
            if isinstance(body_text, str) and body_text.strip():
                return body_text.strip()
            raise ProviderRequestError(
                f"转录端点返回了无法解析的内容（HTTP {status}）: {text[:400]}"
            )
        raise ProviderRequestError(f"转录端点返回类型异常: {type(parsed).__name__}")

    # 鉴权（401/403）与请求体过大（413）跟 response_format 无关：回退换格式只会把同一个
    # 文件原样再传一遍，最后报错还指向第二次尝试，把真正的病因藏起来。
    NO_FALLBACK_STATUS = frozenset({401, 403, 413})

    def _request_transcript(self, target: Path) -> str:
        """发一次 `/audio/transcriptions` 并取回文本；格式按 `_ASR_FORMATS` 依次尝试。

        上游鉴权/配额失败时，把「去查 /audio/transcriptions 配置」这条会**把人带偏**的
        提示换掉——实测网关 `GET /models` 返回 200，而转录全 503 token 获取失败。
        """
        for response_format in self._ASR_FORMATS[:-1]:
            try:
                return self._post_transcription(target, response_format)
            except ProviderRequestError as exc:
                # 4xx = 本次请求（多半是 response_format）不被端点接受 → 换下一种格式再试；
                # 5xx / 无状态码 / 鉴权与体积类 4xx 是端点侧或配置侧故障，换格式没有意义，直接抛出。
                if not exc.status or exc.status >= 500 or exc.status in self.NO_FALLBACK_STATUS:
                    raise
        return self._post_transcription(target, self._ASR_FORMATS[-1])

    def _post_transcription(self, target: Path, response_format: str) -> str:
        """按指定 `response_format` 发一次转录请求并把响应体解析成文本。"""
        fields, files, _ = self.build_transcription_form(target, response_format)
        body, content_type = build_multipart(fields, files)

        headers = self._headers(json_body=False)
        headers["Content-Type"] = content_type

        try:
            status, raw = http_request(
                f"{self.endpoint.base_url}/audio/transcriptions",
                method="POST",
                headers=headers,
                body=body,
                timeout_sec=self.defaults.timeout_sec,
                max_retries=self.defaults.max_retries,
                hint=self._ASR_HINT,
            )
        except ProviderRequestError as exc:
            extra = upstream_hint(getattr(exc, "status", None), getattr(exc, "body", ""))
            if not extra:
                raise
            cleaned = str(exc).replace(self._ASR_HINT, "").rstrip()
            raise ProviderRequestError(
                f"{cleaned}\n{extra}", status=exc.status, body=exc.body
            ) from exc
        return self._extract_transcript(status, raw)

    def _transcribe(self, target: Path) -> str:
        """取逐字稿；**元话语返回会被有限次重试，仍失败就报错**，绝不静默当成功。

        为什么必须拦：外部模型偶尔把「自己的写作计划/自查清单」当结果返回（非空、
        HTTP 200），下游会把它当成真实讲解内容写进教材——静默产出错内容比报错更糟。
        重试次数沿用 `defaults.max_retries`，不新增配置项；成功路径只多一次头部扫描。
        """
        ensure_payload_size(target, self.payload_budget_bytes())
        attempts = max(1, int(self.defaults.max_retries) + 1)
        meta = ""
        for _ in range(attempts):
            text = self._request_transcript(target)
            if not looks_like_model_meta(text):
                return text
            meta = text
        raise ProviderRequestError(
            f"转录端点连续 {attempts} 次返回模型自述的提纲/计划，而不是逐字稿（HTTP 200）: "
            f"{meta[:200]}\n"
            "建议：重试本片，或把 `duration_minutes` 调小（例如 5）后重读本片。"
        )

    def _reason_over_transcript(self, prompt: str, transcript: str) -> Tuple[str, str]:
        """第二段：把逐字稿交给文本模型做总结/问答。"""
        wrapped = TRANSCRIPT_WRAPPER.format(instruction=prompt, transcript=transcript)
        payload = {
            "model": self._text_model(),
            "messages": [{"role": "user", "content": wrapped}],
        }
        data = http_post_json(
            f"{self.endpoint.base_url}/chat/completions",
            payload,
            headers=self._headers(json_body=True),
            timeout_sec=self.defaults.timeout_sec,
            max_retries=self.defaults.max_retries,
            hint=f"第二段纯文本请求失败，请检查 `endpoints.{self.name}.text_model` 是否为可用的对话模型。",
        )
        choices = data.get("choices") or []
        if not choices:
            raise ProviderRequestError(
                f"第二段未返回任何 choices。原始响应摘要: {str(data)[:400]}"
            )
        message = (choices[0] or {}).get("message") or {}
        return (
            extract_text_content(message.get("content")),
            str((choices[0] or {}).get("finish_reason", "") or ""),
        )

    # -- 入口 -------------------------------------------------------------
    def process(
        self,
        media_path: Path,
        prompt: str,
        mode: str,
        transcript: str = "",
    ) -> ProcessingResult:
        target = require_media_file(media_path)
        started = time.monotonic()

        if self.endpoint.openai_mode == "transcriptions":
            asr_text = transcript or self._transcribe(target)
            if mode == "transcribe":
                return ProcessingResult(
                    text=asr_text,
                    endpoint_name=self.name,
                    protocol=self.protocol,
                    model=self.model,
                    elapsed_sec=time.monotonic() - started,
                )
            text, finish_reason = self._reason_over_transcript(prompt, asr_text)
            return ProcessingResult(
                text=text,
                endpoint_name=self.name,
                protocol=self.protocol,
                model=self._text_model(),
                elapsed_sec=time.monotonic() - started,
                finish_reason=finish_reason,
            )

        with ManagedTempDir(prefix="omni_ext_audio_") as tmp_dir:
            text, finish_reason = self._process_chat(target, prompt, tmp_dir)

        return ProcessingResult(
            text=text,
            endpoint_name=self.name,
            protocol=self.protocol,
            model=self.model,
            elapsed_sec=time.monotonic() - started,
            finish_reason=finish_reason,
        )
