"""进程内的仿真外部端点（标准库 http.server，无需网络与任何密钥）。

它在 `127.0.0.1` 上同时提供两种协议的四个路由：

===================================  ==========================================
路由                                 对应协议形状
===================================  ==========================================
`POST /v1beta/models/<m>:generateContent`   Gemini `inlineData`
`POST /v1/chat/completions`                 OpenAI chat（含 `input_audio`）
`POST /v1/audio/transcriptions`             OpenAI 转录（multipart）
`GET  /v1/models`、`GET /v1beta/models`      `status --probe` 用
===================================  ==========================================

它会把每个收到的请求原样记录下来（方法、路径、头、原始字节），测试据此断言
**线上载荷的精确形状**——这是「没有真密钥也能验收协议实现」的关键。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# 逻辑路由名（与具体 URL 解耦，测试里按名字编程响应）
GEMINI_GENERATE = "gemini_generate"
OPENAI_CHAT = "openai_chat"
OPENAI_TRANSCRIPTIONS = "openai_transcriptions"
MODELS = "models"
UNKNOWN = "unknown"


def route_of(path: str) -> str:
    """把请求路径映射到逻辑路由名。"""
    if path.endswith(":generateContent"):
        return GEMINI_GENERATE
    if path.endswith("/chat/completions"):
        return OPENAI_CHAT
    if path.endswith("/audio/transcriptions"):
        return OPENAI_TRANSCRIPTIONS
    if path.endswith("/models"):
        return MODELS
    return UNKNOWN


@dataclass
class RecordedRequest:
    """一条被记录下来的原始请求。"""

    method: str
    path: str
    headers: Dict[str, str]
    body: bytes

    @property
    def route(self) -> str:
        return route_of(self.path)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())


def _default_response(route: str, body: bytes) -> Tuple[int, bytes, str]:
    """各路由的默认响应；正文里带上请求大小，便于确认请求体确实被读到了。"""
    if route == GEMINI_GENERATE:
        payload = {
            "candidates": [
                {
                    "content": {"parts": [{"text": f"STUB-GEMINI 转录结果 ({len(body)} bytes)"}]},
                    "finishReason": "STOP",
                }
            ]
        }
    elif route == OPENAI_CHAT:
        payload = {
            "choices": [
                {
                    "message": {"content": f"STUB-OPENAI-CHAT 结果 ({len(body)} bytes)"},
                    "finish_reason": "stop",
                }
            ]
        }
    elif route == OPENAI_TRANSCRIPTIONS:
        payload = {
            "text": "STUB-ASR 逐字稿：这一段是仿真识别结果。",
            "segments": [
                {"start": 0.0, "text": "STUB-ASR 第一段"},
                {"start": 65.0, "text": "STUB-ASR 第二段"},
            ],
        }
    elif route == MODELS:
        payload = {"data": [], "object": "list"}
    else:
        payload = {"error": f"未知路由: {route}"}
    return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # noqa: D102 - 静音访问日志
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _dispatch(self, method: str) -> None:
        stub: "StubEndpoint" = self.server.stub  # type: ignore[attr-defined]
        body = self._read_body()
        recorded = RecordedRequest(
            method=method,
            path=self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
        )
        stub.requests.append(recorded)

        status, data, content_type = stub.response_for(recorded.route, body)
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # 客户端提前断开
            pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")


class StubEndpoint:
    """仿真端点服务器；用 `with` 或显式 `start()/stop()` 管理生命周期。"""

    def __init__(self) -> None:
        self.requests: List[RecordedRequest] = []
        self._responses: Dict[str, Tuple[int, bytes, str]] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="stub-endpoint", daemon=True
        )
        self._started = False

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "StubEndpoint":
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def stop(self) -> None:
        if self._started:
            self._server.shutdown()
            self._server.server_close()
            self._started = False

    def __enter__(self) -> "StubEndpoint":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -- 地址 -------------------------------------------------------------
    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_gemini(self) -> str:
        """Gemini 协议的 base_url（对应原生 REST 的 /v1beta）。"""
        return f"http://127.0.0.1:{self.port}/v1beta"

    @property
    def base_openai(self) -> str:
        """OpenAI 协议的 base_url（对应 /v1）。"""
        return f"http://127.0.0.1:{self.port}/v1"

    # -- 响应编程 ---------------------------------------------------------
    def set_json(self, route: str, payload: Any, status: int = 200) -> None:
        self._responses[route] = (
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

    def set_raw(self, route: str, data: bytes, status: int = 200, content_type: str = "application/json") -> None:
        self._responses[route] = (status, data, content_type)

    def clear_responses(self) -> None:
        self._responses.clear()

    def response_for(self, route: str, body: bytes) -> Tuple[int, bytes, str]:
        return self._responses.get(route) or _default_response(route, body)

    # -- 请求检索 ---------------------------------------------------------
    def reset_requests(self) -> None:
        self.requests.clear()

    def requests_for(self, route: str) -> List[RecordedRequest]:
        return [r for r in self.requests if r.route == route]
