"""OpenAI-compatible translator for models that write tool calls as XML (MiniCPM5).

    .venv/bin/python -m kernel.voice.toolcall_proxy --upstream http://127.0.0.1:8082/v1 --port 8083

MiniCPM5 answers a tool call with text like
    <function name="schedule_task"><param name="goal">...</param><param name="when">every monday at 8:00</param></function>
which mlx_lm.server passes through as plain content. This proxy sits in front of the model server, lets ordinary text
stream straight through, and turns <function> blocks into standard OpenAI `tool_calls` (streaming and non-streaming),
typing each argument from the request's tool schema. Everything else (requests, tool results, /models) passes as-is.
"""
import argparse
import json
import re
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

FUNC = re.compile(r'<function\s+name="([^"]+)"\s*>(.*?)</function>', re.S)
PARAM = re.compile(r'<param\s+name="([^"]+)"\s*>(.*?)</param>', re.S)


def _types(tools: list | None) -> dict[str, dict[str, str]]:
    out = {}
    for t in tools or []:
        fn = t.get("function", {})
        out[fn.get("name")] = {k: v.get("type", "string") for k, v in (fn.get("parameters", {}).get("properties") or {}).items()}
    return out


def _coerce(value: str, kind: str):
    v = value.strip()
    try:
        if kind == "integer":
            return int(v)
        if kind == "number":
            return float(v)
        if kind == "boolean":
            return v.lower() in ("true", "1", "yes")
        if kind in ("array", "object"):
            return json.loads(v)
    except (ValueError, json.JSONDecodeError):
        pass
    return v


def parse_calls(text: str, tools: list | None) -> tuple[str, list[dict]]:
    """Split model output into (plain text, OpenAI tool_calls)."""
    types = _types(tools)
    calls = []
    for m in FUNC.finditer(text):
        name = m.group(1)
        args = {k: _coerce(v, types.get(name, {}).get(k, "string")) for k, v in PARAM.findall(m.group(2))}
        calls.append({"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                      "function": {"name": name, "arguments": json.dumps(args)}})
    return FUNC.sub("", text).strip(), calls


def build_app(upstream: str) -> FastAPI:
    app = FastAPI()
    client = httpx.AsyncClient(base_url=upstream, timeout=600)

    @app.get("/v1/models")
    async def models():
        r = await client.get("/models")
        return JSONResponse(r.json(), status_code=r.status_code)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        tools = body.get("tools")
        if not body.get("stream"):
            r = await client.post("/chat/completions", json=body)
            data = r.json()
            if r.status_code == 200 and tools:
                msg = data["choices"][0]["message"]
                text, calls = parse_calls(msg.get("content") or "", tools)
                if calls:
                    msg["content"] = text or None
                    msg["tool_calls"] = calls
                    data["choices"][0]["finish_reason"] = "tool_calls"
            return JSONResponse(data, status_code=r.status_code)
        return StreamingResponse(_stream(client, body, tools), media_type="text/event-stream")

    return app


def _chunk(base: dict, delta: dict, finish=None) -> bytes:
    c = {"id": base.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}"), "object": "chat.completion.chunk",
         "created": base.get("created", int(time.time())), "model": base.get("model", ""),
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(c)}\n\n".encode()


async def _stream(client: httpx.AsyncClient, body: dict, tools: list | None):
    """Pass text through; hold back anything from a '<' until we know whether it opens a <function> block."""
    base, held, full, tool_mode, finish = {}, "", "", False, None
    async with client.stream("POST", "/chat/completions", json=body) as r:
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            base = base or chunk
            choice = (chunk.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or finish
            piece = (choice.get("delta") or {}).get("content") or ""
            if not piece:
                if chunk.get("usage"):
                    base["usage"] = chunk["usage"]
                continue
            full += piece
            if tool_mode or not tools:
                if not tools:
                    yield _chunk(base, {"content": piece})
                continue
            held += piece
            i = held.find("<")
            if i < 0:
                yield _chunk(base, {"content": held}); held = ""
            else:
                if i > 0:
                    yield _chunk(base, {"content": held[:i]}); held = held[i:]
                if held.startswith("<function"):
                    tool_mode = True
                elif not "<function".startswith(held[: len("<function")]):
                    yield _chunk(base, {"content": held}); held = ""  # a '<' that isn't a tool call
    if tool_mode:
        _, calls = parse_calls(full, tools)
        for n, c in enumerate(calls):
            yield _chunk(base, {"tool_calls": [{"index": n, "id": c["id"], "type": "function", "function": c["function"]}]})
        yield _chunk(base, {}, "tool_calls")
    else:
        if held:
            yield _chunk(base, {"content": held})
        yield _chunk(base, {}, finish or "stop")
    yield b"data: [DONE]\n\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default="http://127.0.0.1:8082/v1")
    ap.add_argument("--port", type=int, default=8083)
    a = ap.parse_args()
    uvicorn.run(build_app(a.upstream), host="127.0.0.1", port=a.port, log_level="warning")
