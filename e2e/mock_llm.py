#!/usr/bin/env python3
"""A tiny, deterministic OpenAI-compatible server (chat + images) used by the e2e test.

It stands in for a real LLM so the *whole* Open WebUI -> tool-call -> tool-server ->
image path can be exercised without an API key:

* a request that offers tools and has no tool result yet  -> call `generate_image`
* a request whose last message is a tool result            -> answer with its markdown
* anything else (title/tag background tasks)               -> a short plain answer

It also serves POST /v1/images/generations (a canned base64 PNG) so the tool server's real
OpenAI provider code path runs, including its Authorization header.

Every request is recorded and exposed at GET /_requests so the test can assert on it.
Standard library only.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "mock-model"
# A valid 1x1 PNG.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
RECORDED: list[dict] = []
LOCK = threading.Lock()


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):  # multimodal content parts
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


def _final_answer(tool_content: str) -> str:
    try:
        result = json.loads(tool_content)
        markdown = result["markdown"]
    except (ValueError, KeyError, TypeError):
        return f"The tool returned: {tool_content[:500]}"
    return f"Here is your image:\n\n{markdown}"


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-llm/1.0"

    # -- helpers ------------------------------------------------------------
    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks: list[dict]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _chunk(delta: dict, finish: str | None = None) -> dict:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def log_message(self, fmt: str, *args: object) -> None:  # keep container logs quiet
        pass

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "mock"}]})
        elif self.path == "/_requests":
            with LOCK:
                self._json(200, RECORDED)
        elif self.path == "/healthz":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return
        if self.path == "/_reset":
            with LOCK:
                RECORDED.clear()
            self._json(200, {"ok": True})
            return
        if self.path in ("/v1/images/generations", "/images/generations"):
            with LOCK:
                RECORDED.append({"path": self.path, "body": body,
                                 "authorization": self.headers.get("Authorization", "")})
            self._json(200, {"created": 0, "data": [{"b64_json": PNG_B64}]})
            return
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": "not found"})
            return

        with LOCK:
            RECORDED.append({"authorization": self.headers.get("Authorization", ""), "body": body})

        messages = body.get("messages", [])
        tools = body.get("tools") or []
        last_role = messages[-1].get("role") if messages else None
        stream = bool(body.get("stream"))

        if tools and last_role != "tool":
            args = json.dumps(
                {"prompt": _last_user_text(messages) or "a lighthouse", "aspect": "landscape"}
            )
            call = {"index": 0, "id": "call_mock_1", "type": "function",
                    "function": {"name": "generate_image", "arguments": ""}}
            self._sse([
                self._chunk({"role": "assistant", "content": None, "tool_calls": [call]}),
                self._chunk({"tool_calls": [{"index": 0, "function": {"arguments": args}}]}),
                self._chunk({}, "tool_calls"),
            ])
            return

        text = _final_answer(str(messages[-1].get("content", ""))) if last_role == "tool" else "OK"
        if stream:
            self._sse([self._chunk({"role": "assistant", "content": text}), self._chunk({}, "stop")])
        else:
            self._json(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": MODEL_ID,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
