"""Google Gemini 协议端点（REST `models/{model}:generateContent` + `inlineData`）。

只用标准库直连 REST，不依赖 `google-genai` SDK。大文件不走 Files API：切片由本服务
自己控制（默认 10 分钟 16kHz 单声道 ≈ 2~3 MiB），远低于内联上限，因此没有必要为
上传/轮询/删除远端文件引入额外依赖与失败面。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Dict, List

from ..core.limits import AUDIO_EXTS
from .base import (
    BaseEndpoint,
    ProcessingResult,
    ProviderRequestError,
    ensure_payload_size,
    http_post_json,
    require_media_file,
)


def _guess_mime(path: Path) -> str:
    """给出 Gemini 能接受的 mimeType；猜不出时按音频保守兜底。"""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    suffix = path.suffix.lower()
    if suffix in (".m4a", ".mp4", ".aac"):
        return "audio/mp4"
    if suffix in AUDIO_EXTS:
        return "audio/mpeg"
    return "application/octet-stream"


def _join_text(parts: Any) -> str:
    """从 `candidates[0].content.parts` 里取出全部文本。"""
    if not isinstance(parts, list):
        return ""
    collected: List[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                collected.append(text)
    return "\n".join(collected).strip()


class GeminiEndpoint(BaseEndpoint):
    """Gemini `generateContent` 协议。"""

    protocol = "gemini"

    @property
    def inflation(self) -> float:
        """inlineData 走 base64 内联，请求体约为原始媒体字节的 1.37 倍。"""
        return 1.4

    def _url(self) -> str:
        return f"{self.endpoint.base_url}/models/{self.endpoint.model}:generateContent"

    def _headers(self) -> Dict[str, str]:
        headers = self._base_headers()
        # 有 key 才带头：有些自建网关不需要密钥。
        if self.endpoint.api_key and not any(
            k.lower() == "x-goog-api-key" for k in headers
        ):
            headers["x-goog-api-key"] = self.endpoint.api_key
        return headers

    def build_payload(self, media_path: Path, prompt: str) -> Dict[str, Any]:
        """构造请求体（独立成方法，便于单测断言精确形状）。"""
        data_b64 = base64.b64encode(media_path.read_bytes()).decode("ascii")
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": _guess_mime(media_path), "data": data_b64}},
                        {"text": prompt},
                    ],
                }
            ]
        }

    def parse_response(self, data: Dict[str, Any]) -> tuple[str, str]:
        """返回 (正文, finish_reason)；被安全策略拦截时抛错。"""
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            blocked = feedback.get("blockReason")
            if blocked:
                raise ProviderRequestError(
                    f"Gemini 安全策略拦截了本次请求（blockReason={blocked}）。"
                    f"请检查媒体内容或换用其他端点。"
                )
            raise ProviderRequestError(
                f"Gemini 未返回任何候选结果。原始响应: {json.dumps(data, ensure_ascii=False)[:400]}"
            )

        first = candidates[0] if isinstance(candidates[0], dict) else {}
        text = _join_text((first.get("content") or {}).get("parts"))
        if not text:
            finish = first.get("finishReason", "")
            raise ProviderRequestError(
                f"Gemini 返回了空文本（finishReason={finish or '未知'}）。"
                f"若为 MAX_TOKENS，请调小 duration_minutes 后重试本片。"
            )
        return text, str(first.get("finishReason", "") or "")

    def process(
        self,
        media_path: Path,
        prompt: str,
        mode: str,
        transcript: str = "",
    ) -> ProcessingResult:
        target = require_media_file(media_path)
        ensure_payload_size(target, self.payload_budget_bytes())

        started = time.monotonic()
        data = http_post_json(
            self._url(),
            self.build_payload(target, prompt),
            headers=self._headers(),
            timeout_sec=self.defaults.timeout_sec,
            max_retries=self.defaults.max_retries,
            hint=(
                "请检查 `base_url`（Gemini 原生应为 .../v1beta）与 `model` 名称是否正确。"
            ),
        )
        text, finish_reason = self.parse_response(data)

        return ProcessingResult(
            text=text,
            endpoint_name=self.name,
            protocol=self.protocol,
            model=self.model,
            elapsed_sec=time.monotonic() - started,
            finish_reason=finish_reason,
        )
