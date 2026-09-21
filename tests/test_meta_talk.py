"""上游失败提示与转录返回的回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni_media.config import Defaults, Endpoint
from omni_media.providers.base import (
    ProviderRequestError,
    upstream_hint,
)
from omni_media.providers.openai import OpenAIEndpoint

from stub_endpoint import OPENAI_TRANSCRIPTIONS


# ---------------------------------------------------------------------------
# 上游失败提示
# ---------------------------------------------------------------------------

def test_upstream_hint_matches_gateway_token_failure():
    hint = upstream_hint(503, "Token acquisition timeout (5s) - system too busy or deadlock detected")
    assert "上游" in hint
    assert "/audio/transcriptions" in hint  # 明确告诉操作者**不要**去查这个


@pytest.mark.parametrize(
    "status, body",
    [
        (503, "multipart 解析失败"),          # 5xx 但不是上游凭证问题
        (401, "unauthorized"),                 # 上游特征命中，但不是 5xx（配置/密钥错）
        (400, "bad request"),                  # 4xx
        (200, ""),                             # 成功
    ],
)
def test_upstream_hint_ignores_other_failures(status: int, body: str):
    assert upstream_hint(status, body) == ""


# ---------------------------------------------------------------------------
# _transcribe：端点正常返回逐字稿
# ---------------------------------------------------------------------------

def _transcriptions_endpoint(base_url: str, max_retries: int) -> OpenAIEndpoint:
    return OpenAIEndpoint(
        Endpoint(
            name="oai",
            protocol="openai",
            base_url=base_url,
            model="gemini-3.8-flash-high",
            api_key="test-key",
            openai_mode="transcriptions",
            language="zh",
        ),
        Defaults(
            slice_minutes=10.0,
            max_payload_mb=18,
            timeout_sec=30,
            max_retries=max_retries,
        ),
    )


def test_transcribe_happy_path_unchanged(stub, audio_m4a: Path):
    """正常返回仍是单次请求、原样返回（成功路径零新增开销）。"""
    endpoint = _transcriptions_endpoint(stub.base_openai, max_retries=2)
    result = endpoint.process(audio_m4a, "请转录", "transcribe")

    assert len(stub.requests_for(OPENAI_TRANSCRIPTIONS)) == 1
    assert "STUB-ASR" in result.text
