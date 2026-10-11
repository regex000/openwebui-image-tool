#!/usr/bin/env python3
"""Assertions for the e2e run (standard library only). Configured via E2E_* variables.

Flow: sign in to Open WebUI -> start a chat with the image tool enabled -> the mock LLM
requests `generate_image` -> Open WebUI calls the tool server with its Bearer key ->
the model replies with the returned markdown -> we download the image.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

WEBUI = os.environ["E2E_WEBUI_URL"].rstrip("/")
TOOL = os.environ["E2E_TOOL_URL"].rstrip("/")
MOCK = os.environ["E2E_MOCK_URL"].rstrip("/")
EMAIL = os.environ["E2E_EMAIL"]
PASSWORD = os.environ["E2E_PASSWORD"]
TOOL_ID = "server:image-tool"
MODEL = "mock-model"
PROMPT = "A watercolor lighthouse at dawn"  # the mock LLM forwards this as the tool prompt


def request(method, url, *, body=None, headers=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def as_json(raw: bytes):
    return json.loads(raw.decode() or "null")


def step(title: str) -> None:
    print(f"  - {title}", flush=True)


def fail(what: str, **context) -> None:
    print(f"\nFAILED: {what}", file=sys.stderr)
    for key, value in context.items():
        print(f"--- {key} ---\n{json.dumps(value, indent=2)[:3000]}", file=sys.stderr)
    sys.exit(1)


def expect(condition: bool, what: str, **context) -> None:
    if not condition:
        fail(what, **context)


def main() -> None:
    print("Open WebUI end-to-end test")

    step("tool server is unauthenticated-safe (401 without / with a wrong key)")
    for headers in ({}, {"Authorization": "Bearer wrong-key"}):
        status, _, _ = request("POST", f"{TOOL}/generate_image", body={"prompt": "x"}, headers=headers)
        expect(status == 401, f"expected 401 from tool server, got {status}")

    step("public sign-up is closed")
    status, _, _ = request("POST", f"{WEBUI}/api/v1/auths/signup", body={
        "name": "Intruder", "email": "intruder@example.com", "password": "Passw0rd!x1"})
    expect(status in (401, 403), f"sign-up should be refused, got {status}")

    step("admin signs in to Open WebUI (account was created from env vars)")
    status, _, raw = request("POST", f"{WEBUI}/api/v1/auths/signin",
                             body={"email": EMAIL, "password": PASSWORD})
    expect(status == 200, f"sign-in failed with {status}", body=raw.decode()[:300])
    auth = {"Authorization": f"Bearer {as_json(raw)['token']}"}

    step("Open WebUI lists the mock chat model")
    status, _, raw = request("GET", f"{WEBUI}/api/models", headers=auth)
    models = [m["id"] for m in as_json(raw).get("data", [])]
    expect(status == 200 and MODEL in models, "mock model missing", models=models)

    step("Open WebUI imported the tool server from TOOL_SERVER_CONNECTIONS")
    status, _, raw = request("GET", f"{WEBUI}/api/v1/tools/", headers=auth)
    tool_ids = [t.get("id") for t in as_json(raw)] if status == 200 else []
    expect(TOOL_ID in tool_ids, "tool server not registered", tool_ids=tool_ids)

    request("POST", f"{MOCK}/_reset")

    step("create a chat and ask for an image with the tool enabled")
    user_id, assistant_id, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    chat = {"chat": {
        "title": "e2e", "models": [MODEL],
        "history": {"currentId": assistant_id, "messages": {
            user_id: {"id": user_id, "role": "user", "content": PROMPT, "timestamp": now,
                      "models": [MODEL], "childrenIds": [assistant_id]},
            assistant_id: {"id": assistant_id, "role": "assistant", "content": "", "parentId": user_id,
                           "childrenIds": [], "model": MODEL, "modelName": MODEL, "modelIdx": 0,
                           "done": False, "timestamp": now + 1},
        }},
    }}
    status, _, raw = request("POST", f"{WEBUI}/api/v1/chats/new", body=chat, headers=auth)
    expect(status == 200, f"chat creation failed with {status}", body=raw.decode()[:500])
    chat_id = as_json(raw)["id"]

    # chat_id + id + stream=true makes Open WebUI run the whole native tool loop server-side.
    status, _, raw = request("POST", f"{WEBUI}/api/chat/completions", headers=auth, body={
        "model": MODEL, "stream": True, "chat_id": chat_id, "id": assistant_id,
        "messages": [{"role": "user", "content": PROMPT}],
        "tool_ids": [TOOL_ID],
        "background_tasks": {"title_generation": False, "tags_generation": False,
                             "follow_up_generation": False},
    })
    expect(status == 200, f"chat completion failed with {status}", body=raw.decode()[:500])

    step("wait for the assistant reply containing the image")
    content, message = "", {}
    for _ in range(60):
        status, _, raw = request("GET", f"{WEBUI}/api/v1/chats/{chat_id}", headers=auth)
        message = as_json(raw)["chat"]["history"]["messages"].get(assistant_id, {})
        content = message.get("content") or ""
        if "![" in content and message.get("done"):
            break
        time.sleep(1)
    expect("![" in content and "/images/" in content, "no markdown image in the reply", message=message)

    step("the mock LLM saw a tool offer, then the tool result with an image URL")
    _, _, raw = request("GET", f"{MOCK}/_requests")
    recorded = as_json(raw)
    with_tools = [r for r in recorded if r["body"].get("tools")]  # chat calls only
    expect(len(with_tools) >= 2, "expected two tool-enabled upstream calls", calls=len(recorded))
    names = [t["function"]["name"] for t in with_tools[0]["body"]["tools"]]
    expect("generate_image" in names, "generate_image was not offered to the model", names=names)
    tool_messages = [m for m in with_tools[-1]["body"]["messages"] if m.get("role") == "tool"]
    expect(bool(tool_messages), "second upstream call did not include a tool result")
    result = json.loads(tool_messages[-1]["content"])
    expect(result.get("provider") == "openai" and result.get("image_url"), "unexpected tool result",
           result=result)

    step("the tool server authenticated to the image API with its own provider key")
    image_calls = [r for r in recorded if r.get("path", "").endswith("/images/generations")]
    expect(len(image_calls) == 1, "expected exactly one image-API call", calls=len(image_calls))
    call = image_calls[0]
    expect(call["authorization"] == "Bearer mock-image-key",
           "image API did not receive the provider key", header=call["authorization"][:20])
    expect(call["body"].get("model") == "gpt-image-2" and call["body"].get("size") == "1536x1024"
           and call["body"].get("prompt") == PROMPT, "unexpected image API payload", body=call["body"])

    step("the generated image downloads through its signed URL")
    image_path = "/images/" + result["image_url"].split("/images/", 1)[1]
    status, headers, raw = request("GET", TOOL + image_path)
    expect(status == 200 and raw.startswith(b"\x89PNG"), f"image download failed ({status})")
    status, _, _ = request("GET", TOOL + image_path[:-1] + "x")
    expect(status == 403, f"tampered link was not rejected ({status})")

    print("\nPASS: Open WebUI called the authenticated tool server and displayed the image.")
    print(f"      image URL: {result['image_url'].split('?')[0]}")


if __name__ == "__main__":
    main()
