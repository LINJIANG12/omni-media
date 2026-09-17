"""协议层测试：三种线上载荷的精确形状 + 响应解析分支 + 切片规划。

这些测试**不需要任何真实密钥**：请求打到进程内的仿真端点（`stub_endpoint.py`），
断言的是发出去的原始字节，因此协议改坏了会立刻暴露。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from omni_media_ext.config import Defaults, Endpoint
from omni_media_ext.core.preprocessor import MediaPreprocessor
from omni_media_ext.core.temp_manager import ManagedTempDir
from omni_media_ext.providers.base import (
    ProviderRequestError,
    build_multipart,
    ensure_payload_size,
    extract_text_content,
)
from omni_media_ext.providers.gemini import GeminiEndpoint, _guess_mime
from omni_media_ext.providers.openai import OpenAIEndpoint
from omni_media_ext.server import plan_slice
from stub_endpoint import (
    GEMINI_GENERATE,
    MODELS,
    OPENAI_CHAT,
    OPENAI_TRANSCRIPTIONS,
    route_of,
)


# ---------------------------------------------------------------------------
# 构造助手
# ---------------------------------------------------------------------------

def make_defaults(**overrides) -> Defaults:
    values = dict(
        slice_minutes=10.0,
        max_payload_mb=18,
        timeout_sec=30,
        max_retries=0,  # 测试里不重试，保持单次请求可断言
        max_concurrency=2,
    )
    values.update(overrides)
    return Defaults(**values)


def gemini_endpoint(base_url: str, model: str = "gemini-test") -> GeminiEndpoint:
    return GeminiEndpoint(
        Endpoint(
            name="gem",
            protocol="gemini",
            base_url=base_url,
            model=model,
            api_key="test-gemini-key-1234567890",
        ),
        make_defaults(),
    )


def openai_endpoint(
    base_url: str,
    model: str = "gpt-4o-audio-preview",
    mode: str = "chat",
    **extra,
) -> OpenAIEndpoint:
    return OpenAIEndpoint(
        Endpoint(
            name="oai",
            protocol="openai",
            base_url=base_url,
            model=model,
            api_key="test-openai-key-1234567890",
            openai_mode=mode,
            **extra,
        ),
        make_defaults(),
    )


# ---------------------------------------------------------------------------
# 路由识别（stub 自身也要被测，否则断言建立在一个错误的分派上）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, expected",
    [
        ("/v1beta/models/gemini-2.5-flash:generateContent", GEMINI_GENERATE),
        ("/v1/chat/completions", OPENAI_CHAT),
        ("/v1/audio/transcriptions", OPENAI_TRANSCRIPTIONS),
        ("/v1/models", MODELS),
        ("/weird", "unknown"),
    ],
)
def test_stub_route_matching(path, expected):
    assert route_of(path) == expected


# ---------------------------------------------------------------------------
# Gemini 协议
# ---------------------------------------------------------------------------

def test_gemini_payload_shape(audio_m4a: Path):
    endpoint = gemini_endpoint("https://example.invalid/v1beta")
    payload = endpoint.build_payload(audio_m4a, "请转录")

    parts = payload["contents"][0]["parts"]
    assert payload["contents"][0]["role"] == "user"
    assert len(parts) == 2
    assert parts[0]["inlineData"]["mimeType"] == "audio/mp4"
    assert base64.b64decode(parts[0]["inlineData"]["data"]) == audio_m4a.read_bytes()
    assert parts[1]["text"] == "请转录"


def test_gemini_wire_request(stub, audio_m4a: Path):
    endpoint = gemini_endpoint(stub.base_gemini)
    result = endpoint.process(audio_m4a, "请转录", "transcribe")

    # URL 与鉴权头
    request = stub.requests_for(GEMINI_GENERATE)[0]
    assert request.path == "/v1beta/models/gemini-test:generateContent"
    assert request.header("x-goog-api-key") == "test-gemini-key-1234567890"
    assert request.header("content-type") == "application/json"

    # 载荷确实是 inlineData + text
    body = request.json()
    assert body["contents"][0]["parts"][0]["inlineData"]["data"]
    assert body["contents"][0]["parts"][1]["text"] == "请转录"

    # 结果解析
    assert "STUB-GEMINI" in result.text
    assert result.endpoint_name == "gem"
    assert result.protocol == "gemini"
    assert result.finish_reason == "STOP"
    assert result.elapsed_sec >= 0


def test_gemini_blocked_response_raises(stub, audio_m4a: Path):
    stub.set_json(GEMINI_GENERATE, {"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(ProviderRequestError) as excinfo:
        gemini_endpoint(stub.base_gemini).process(audio_m4a, "p", "transcribe")
    assert "blockReason=SAFETY" in str(excinfo.value)


def test_gemini_empty_candidates_raises(stub, audio_m4a: Path):
    stub.set_json(GEMINI_GENERATE, {"candidates": []})
    with pytest.raises(ProviderRequestError) as excinfo:
        gemini_endpoint(stub.base_gemini).process(audio_m4a, "p", "transcribe")
    assert "未返回任何候选" in str(excinfo.value)


def test_gemini_empty_text_raises(stub, audio_m4a: Path):
    stub.set_json(GEMINI_GENERATE, {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]})
    with pytest.raises(ProviderRequestError) as excinfo:
        gemini_endpoint(stub.base_gemini).process(audio_m4a, "p", "transcribe")
    assert "MAX_TOKENS" in str(excinfo.value)


def test_gemini_payload_budget_accounts_for_base64_inflation():
    """base64 内联会把请求体放大，预算必须先折算，否则 HTTP 层才报错。"""
    endpoint = gemini_endpoint("https://example.invalid/v1beta")
    assert endpoint.inflation > 1.0
    assert endpoint.payload_budget_bytes() < endpoint.defaults.max_payload_bytes


def test_error_body_is_excerpted_not_dumped(stub, audio_m4a: Path):
    stub.set_raw(GEMINI_GENERATE, b"x" * 5000, status=400, content_type="text/plain")
    with pytest.raises(ProviderRequestError) as excinfo:
        gemini_endpoint(stub.base_gemini).process(audio_m4a, "p", "transcribe")
    message = str(excinfo.value)
    assert "HTTP 400" in message
    assert "已截断" in message
    assert len(message) < 1200


def test_auth_errors_are_not_retried(stub, audio_m4a: Path):
    stub.set_raw(GEMINI_GENERATE, b'{"error":"unauthorized"}', status=401)
    # max_retries=0 已足够；这里额外确认 401 不会进入重试分支（只发一次请求）
    with pytest.raises(ProviderRequestError):
        gemini_endpoint(stub.base_gemini).process(audio_m4a, "p", "transcribe")
    assert len(stub.requests_for(GEMINI_GENERATE)) == 1


def test_guess_mime_variants(tmp_path: Path):
    assert _guess_mime(tmp_path / "a.m4a") == "audio/mp4"
    assert _guess_mime(tmp_path / "a.mp3") == "audio/mpeg"
    assert _guess_mime(tmp_path / "a.unknownext") == "application/octet-stream"


# ---------------------------------------------------------------------------
# OpenAI chat 协议
# ---------------------------------------------------------------------------

def test_openai_chat_payload_shape():
    endpoint = openai_endpoint("https://example.invalid/v1")
    payload = endpoint.build_chat_payload("QUJD", "mp3", "请转录")

    content = payload["messages"][0]["content"]
    assert payload["model"] == "gpt-4o-audio-preview"
    assert content[0] == {"type": "text", "text": "请转录"}
    assert content[1] == {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "mp3"}}
    # audio 模型才带 modalities，通用兼容端点不能被塞这个字段
    assert payload["modalities"] == ["text"]


def test_openai_chat_omits_modalities_for_generic_models():
    endpoint = openai_endpoint("https://example.invalid/v1", model="some-gateway-model")
    assert "modalities" not in endpoint.build_chat_payload("QUJD", "mp3", "p")


def test_openai_chat_wire_transcodes_m4a_to_real_mp3(stub, audio_m4a: Path):
    endpoint = openai_endpoint(stub.base_openai)
    result = endpoint.process(audio_m4a, "请转录", "transcribe")

    request = stub.requests_for(OPENAI_CHAT)[0]
    assert request.path == "/v1/chat/completions"
    assert request.header("authorization") == "Bearer test-openai-key-1234567890"

    audio_block = request.json()["messages"][0]["content"][1]
    assert audio_block["type"] == "input_audio"
    assert audio_block["input_audio"]["format"] == "mp3"

    decoded = base64.b64decode(audio_block["input_audio"]["data"])
    # 真转码的证据：MP3 要么以 ID3v2 标签开头，要么以帧同步字 0xFFEx 开头；
    # 而 m4a 一定以 ftyp box（b"\x00\x00\x00 "）开头。
    is_id3 = decoded.startswith(b"ID3")
    is_frame_sync = decoded[0] == 0xFF and (decoded[1] & 0xE0) == 0xE0
    assert is_id3 or is_frame_sync, f"不像 MP3 字节: {decoded[:8]!r}"
    assert not decoded.startswith(b"\x00\x00\x00")
    assert b"ftyp" not in decoded[:32]
    assert "STUB-OPENAI-CHAT" in result.text


def test_openai_chat_reuses_mp3_source_without_transcoding(tmp_path: Path, audio_m4a: Path):
    """源已是 mp3 时不应再转码（省一次 ffmpeg，也避免二次损失）。"""
    mp3 = MediaPreprocessor.transcode_to_mp3(audio_m4a, tmp_path / "src.mp3")
    endpoint = openai_endpoint("https://example.invalid/v1")

    with ManagedTempDir() as tmp:
        data_b64, fmt = endpoint._encode_audio(mp3, Path(tmp))
    assert fmt == "mp3"
    assert base64.b64decode(data_b64) == mp3.read_bytes()


def test_openai_chat_non_audio_model_still_sends_text_and_audio(stub, audio_m4a: Path):
    endpoint = openai_endpoint(stub.base_openai, model="gateway-audio-v2")
    endpoint.process(audio_m4a, "p", "qa")
    content = stub.requests_for(OPENAI_CHAT)[0].json()["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text", "input_audio"]


def test_openai_empty_choices_raises(stub, audio_m4a: Path):
    stub.set_json(OPENAI_CHAT, {"choices": []})
    with pytest.raises(ProviderRequestError) as excinfo:
        openai_endpoint(stub.base_openai).process(audio_m4a, "p", "transcribe")
    assert "choices" in str(excinfo.value)


def test_openai_null_content_raises(stub, audio_m4a: Path):
    stub.set_json(OPENAI_CHAT, {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]})
    with pytest.raises(ProviderRequestError) as excinfo:
        openai_endpoint(stub.base_openai).process(audio_m4a, "p", "transcribe")
    assert "空的 message.content" in str(excinfo.value)


def test_openai_list_content_is_joined(stub, audio_m4a: Path):
    stub.set_json(
        OPENAI_CHAT,
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "第一行"},
                            {"type": "text", "text": "第二行"},
                        ]
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )
    result = openai_endpoint(stub.base_openai).process(audio_m4a, "p", "summarize")
    assert result.text == "第一行\n第二行"


# ---------------------------------------------------------------------------
# OpenAI transcriptions 协议
# ---------------------------------------------------------------------------

def test_openai_transcription_form_shape(audio_m4a: Path):
    endpoint = openai_endpoint("https://example.invalid/v1", model="whisper-1", mode="transcriptions")
    fields, files, mime = endpoint.build_transcription_form(audio_m4a)

    assert ("model", "whisper-1") in fields
    # 默认要 verbose_json：端点会连同 segments 一起返回，我们据此渲染行首时间戳
    assert ("response_format", "verbose_json") in fields
    # 回退格式必须仍然可用（端点不认 verbose_json 时走这一条）
    assert ("response_format", "json") in endpoint.build_transcription_form(audio_m4a, "json")[0]
    name, filename, content_type, data = files[0]
    assert name == "file"
    assert filename == audio_m4a.name
    assert content_type == "audio/mp4" and mime == "audio/mp4"
    assert data == audio_m4a.read_bytes()


def test_openai_transcription_falls_back_to_plain_json(audio_m4a: Path, monkeypatch):
    """端点不认 verbose_json（4xx）时必须退回 json 再试一次，而不是让整门课停在这里。"""
    from omni_media_ext.providers import openai as openai_mod

    endpoint = openai_endpoint("https://example.invalid/v1", model="whisper-1", mode="transcriptions")
    bodies = []

    def fake_http_request(url, method="GET", headers=None, body=b"", **kwargs):
        bodies.append(body)
        if len(bodies) == 1:
            raise ProviderRequestError(
                "400 不支持的 response_format", status=400, body="bad response_format"
            )
        return 200, json.dumps({"text": "退化为纯文本的逐字稿"}).encode("utf-8")

    monkeypatch.setattr(openai_mod, "http_request", fake_http_request)
    result = endpoint.process(audio_m4a, "请转录", "transcribe")

    assert result.text == "退化为纯文本的逐字稿"
    assert len(bodies) == 2, "verbose_json 被拒后应恰好再试一次"
    assert b'name="response_format"\r\n\r\nverbose_json\r\n' in bodies[0]
    assert b'name="response_format"\r\n\r\njson\r\n' in bodies[1]


def test_openai_transcript_renders_segments_and_plain_text():
    """段级返回渲染成行首时间戳；只回纯文本时也要能出稿（时间戳降级为无）。"""
    plain = json.dumps({"text": "纯文本逐字稿"}).encode("utf-8")
    assert OpenAIEndpoint._extract_transcript(200, plain) == "纯文本逐字稿"

    segs = json.dumps(
        {"segments": [{"start": 0.0, "text": "第一段"}, {"start": 65.0, "text": "第二段"}]}
    ).encode("utf-8")
    rendered = OpenAIEndpoint._extract_transcript(200, segs)
    assert rendered.splitlines() == ["[00:00:00] 第一段", "[00:01:05] 第二段"]


def test_multipart_builder_structure():
    body, content_type = build_multipart([("model", "whisper-1")], [("file", "a.m4a", "audio/mp4", b"RAW")])
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1].encode()
    assert body.count(b"--" + boundary) == 3  # 两个分隔 + 一个结束
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="file"; filename="a.m4a"' in body
    assert b"Content-Type: audio/mp4" in body
    assert body.endswith(b"--" + boundary + b"--\r\n")


def test_openai_transcriptions_wire_is_multipart(stub, audio_m4a: Path):
    endpoint = openai_endpoint(stub.base_openai, model="whisper-1", mode="transcriptions", language="zh")
    result = endpoint.process(audio_m4a, "请转录", "transcribe")

    request = stub.requests_for(OPENAI_TRANSCRIPTIONS)[0]
    assert request.path == "/v1/audio/transcriptions"
    assert request.header("content-type").startswith("multipart/form-data; boundary=")
    assert request.header("authorization") == "Bearer test-openai-key-1234567890"

    raw = request.text()
    assert 'name="file"; filename="sample_16k.m4a"' in raw
    assert 'name="model"' in raw and "whisper-1" in raw
    assert 'name="language"' in raw and "zh" in raw
    # multipart 是原样上传，不存在 base64 膨胀
    assert endpoint.inflation == 1.0

    # 逐字稿模式：只有一段请求，且 segments 被渲染成时间戳
    assert len(stub.requests) == 1
    assert "[00:00:00] STUB-ASR 第一段" in result.text
    assert "[00:01:05] STUB-ASR 第二段" in result.text


def test_openai_transcriptions_two_stage_for_summarize(stub, audio_m4a: Path):
    """transcriptions 模式 + 非转录任务 = ASR 一段 + 纯文本推理一段。"""
    endpoint = openai_endpoint(
        stub.base_openai,
        model="whisper-1",
        mode="transcriptions",
        text_model="gpt-4o-mini",
    )
    result = endpoint.process(audio_m4a, "请总结本讲", "summarize")

    assert len(stub.requests) == 2
    assert stub.requests[0].route == OPENAI_TRANSCRIPTIONS
    assert stub.requests[1].route == OPENAI_CHAT

    second = stub.requests[1].json()
    assert second["model"] == "gpt-4o-mini"
    content = second["messages"][0]["content"]
    # 第二段必须是纯文本：不能再带 input_audio
    assert isinstance(content, str)
    assert "STUB-ASR" in content
    assert "请总结本讲" in content
    assert result.model == "gpt-4o-mini"
    assert "STUB-OPENAI-CHAT" in result.text


def test_openai_transcriptions_requires_text_model(stub, audio_m4a: Path):
    from omni_media_ext.config import ConfigError

    endpoint = openai_endpoint(stub.base_openai, model="whisper-1", mode="transcriptions")
    with pytest.raises(ConfigError) as excinfo:
        endpoint.process(audio_m4a, "请总结", "summarize")
    assert "text_model" in str(excinfo.value)


def test_openai_transcriptions_empty_text_raises(stub, audio_m4a: Path):
    stub.set_json(OPENAI_TRANSCRIPTIONS, {"text": "   "})
    endpoint = openai_endpoint(stub.base_openai, model="whisper-1", mode="transcriptions")
    with pytest.raises(ProviderRequestError):
        endpoint.process(audio_m4a, "p", "transcribe")


# ---------------------------------------------------------------------------
# 文本归一化与载荷守卫
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content, expected",
    [
        ("hello", "hello"),
        ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "a\nb"),
        ([{"content": "c"}], "c"),
        (["x", "y"], "x\ny"),
        ({"text": "d"}, "d"),
    ],
)
def test_extract_text_content_variants(content, expected):
    assert extract_text_content(content) == expected


@pytest.mark.parametrize("content", [None, [], [{"type": "image"}], {}, 42])
def test_extract_text_content_rejects_unusable(content):
    with pytest.raises(ProviderRequestError):
        extract_text_content(content)


def test_ensure_payload_size_guard(tmp_path: Path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * 2048)
    assert ensure_payload_size(big, 4096) == 2048
    with pytest.raises(ValueError) as excinfo:
        ensure_payload_size(big, 1024)
    assert "duration_minutes" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 切片规划
# ---------------------------------------------------------------------------

def test_plan_slice_defaults_to_configured_minutes():
    plan = plan_slice(3600.0, 0.0, None, 10.0, 18 * 1024 * 1024)
    assert plan["slice_seconds"] == 600.0
    assert plan["has_next"] is True
    assert plan["clamped"] is False
    assert plan["effective_minutes"] == 10.0


def test_plan_slice_marks_last_chunk_as_finished():
    plan = plan_slice(300.0, 0.0, 10.0, 10.0, 18 * 1024 * 1024)
    assert plan["has_next"] is False
    assert plan["end_sec"] == 300.0


def test_plan_slice_clamps_to_payload_budget():
    # 4000 字节/秒 → 1MiB 只够 262 秒
    plan = plan_slice(3600.0, 0.0, 60.0, 10.0, 1024 * 1024)
    assert plan["clamped"] is True
    assert plan["slice_seconds"] <= 1024 * 1024 / 4000 + 1
    assert plan["requested_minutes"] == 60.0


def test_plan_slice_unknown_duration_has_no_pagination():
    plan = plan_slice(0.0, 0.0, 5.0, 10.0, 18 * 1024 * 1024)
    assert plan["duration_known"] is False
    assert plan["has_next"] is False
    assert plan["slice_seconds"] == 300.0


def test_plan_slice_midway_start_reports_next():
    plan = plan_slice(1800.0, 600.0, 10.0, 10.0, 18 * 1024 * 1024)
    assert plan["end_sec"] == 1200.0
    assert plan["has_next"] is True


def test_json_status_payload_is_machine_readable(stub, audio_m4a: Path):
    """provider 层只负责返回正文，不掺任何状态注释（状态由 server 层统一拼）。"""
    endpoint = gemini_endpoint(stub.base_gemini)
    result = endpoint.process(audio_m4a, "请转录", "transcribe")
    assert "OMNI_STATUS" not in result.text
    assert "<!--" not in result.text
