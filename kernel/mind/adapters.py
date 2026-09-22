"""Provider adapters for Guppy's Mind: one interface over Claude Code, Codex, and Grok Build CLIs.

Each adapter drives the *unmodified* CLI binary, logged in with the Admiral's own subscription, over its
native streaming protocol, and normalizes what it sees into BrainEvents:

    claude  claude -p --input-format stream-json --output-format stream-json   (NDJSON)
    codex   codex app-server                                                  (JSON-RPC over stdio)
    grok    grok agent --no-leader stdio                                       (ACP JSON-RPC over stdio)

One process per task: tasks are long-running background work, so a ~3-5s cold start doesn't matter, and it
keeps tasks isolated. (The voice path never waits on the Mind.)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class BrainEvent:
    type: str            # ready | text_delta | tool_start | tool_end | turn_end | error
    data: dict[str, Any] = field(default_factory=dict)


class BrainAdapter:
    id = "base"

    def __init__(self, *, cwd: str, instructions: str, model: str | None = None, effort: str | None = None,
                 autonomy: str = "bypass"):
        self.cwd, self.instructions, self.model, self.effort, self.autonomy = cwd, instructions, model, effort, autonomy
        self.proc: asyncio.subprocess.Process | None = None

    @classmethod
    def available(cls) -> bool:
        return shutil.which(cls.binary) is not None

    async def _spawn(self, *args: str):
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024 * 1024, env={**os.environ, "NO_COLOR": "1"})

    async def _write(self, obj: dict):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _lines(self) -> AsyncIterator[dict]:
        while line := await self.proc.stdout.readline():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    async def run(self, goal: str) -> AsyncIterator[BrainEvent]:
        raise NotImplementedError
        yield

    async def interrupt(self):
        await self.close()

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()


class ClaudeAdapter(BrainAdapter):
    id, binary = "claude", "claude"

    async def run(self, goal: str) -> AsyncIterator[BrainEvent]:
        args = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--append-system-prompt", self.instructions]
        if self.autonomy == "bypass":
            args += ["--permission-mode", "bypassPermissions"]
        if self.model:
            args += ["--model", self.model]
        await self._spawn(*args)
        await self._write({"type": "user", "message": {"role": "user", "content": goal}})
        self.proc.stdin.close()  # single turn per task
        async for d in self._lines():
            t = d.get("type")
            if t == "system" and d.get("subtype") == "init":
                yield BrainEvent("ready", {"session_id": d.get("session_id"), "model": d.get("model")})
            elif t == "stream_event":
                delta = d.get("event", {}).get("delta", {})
                if delta.get("type") == "text_delta":
                    yield BrainEvent("text_delta", {"text": delta.get("text", "")})
            elif t == "assistant":
                for c in d.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        yield BrainEvent("tool_start", {"id": c.get("id"), "name": c.get("name"), "input": c.get("input")})
            elif t == "user":
                content = d.get("message", {}).get("content", [])
                for c in content if isinstance(content, list) else []:
                    if c.get("type") == "tool_result":
                        yield BrainEvent("tool_end", {"id": c.get("tool_use_id"), "ok": not c.get("is_error")})
            elif t == "result":
                yield BrainEvent("turn_end", {"ok": not d.get("is_error"), "text": d.get("result") or "",
                                              "cost_usd": d.get("total_cost_usd"), "session_id": d.get("session_id")})
                return
        yield BrainEvent("error", {"message": f"claude exited ({await self.proc.wait()})"})


class CodexAdapter(BrainAdapter):
    id, binary = "codex", "codex"

    async def run(self, goal: str) -> AsyncIterator[BrainEvent]:
        await self._spawn("codex", "app-server")
        rid = 0

        async def req(method, params):
            nonlocal rid
            rid += 1
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return rid

        bypass = self.autonomy == "bypass"
        await req("initialize", {"clientInfo": {"name": "guppy", "version": "0.1"}})
        thread_id, last_message = None, ""
        async for d in self._lines():
            m = d.get("method")
            if d.get("id") == 1 and "result" in d:
                await self._write({"jsonrpc": "2.0", "method": "initialized"})
                params = {"cwd": self.cwd, "developerInstructions": self.instructions,
                          "approvalPolicy": "never" if bypass else "on-request",
                          "sandbox": "danger-full-access" if bypass else "workspace-write"}
                if self.model:
                    params["model"] = self.model
                await req("thread/start", params)
            elif d.get("id") == 2 and "result" in d:
                thread_id = d["result"]["thread"]["id"]
                yield BrainEvent("ready", {"session_id": thread_id})
                turn = {"threadId": thread_id, "input": [{"type": "text", "text": goal}]}
                if self.effort:
                    turn["effort"] = self.effort
                await req("turn/start", turn)
            elif "error" in d and "id" in d and not m:
                yield BrainEvent("error", {"message": json.dumps(d["error"])[:500]})
                return
            elif m and "id" in d and m.endswith("requestApproval"):  # server->client approval request
                await self._write({"jsonrpc": "2.0", "id": d["id"],
                                   "result": {"decision": "acceptForSession" if bypass else "decline"}})
            elif m == "item/agentMessage/delta":
                yield BrainEvent("text_delta", {"text": d["params"].get("delta", "")})
            elif m == "item/started":
                item = d["params"].get("item", {})
                if item.get("type") not in ("agentMessage", "userMessage", "reasoning"):
                    yield BrainEvent("tool_start", {"id": item.get("id"), "name": item.get("type"),
                                                    "input": item.get("command") or item.get("changes") or item.get("tool")})
            elif m == "item/completed":
                item = d["params"].get("item", {})
                if item.get("type") == "agentMessage":
                    last_message = item.get("text", last_message)
                elif item.get("type") not in ("userMessage", "reasoning"):
                    yield BrainEvent("tool_end", {"id": item.get("id"), "ok": item.get("status") != "failed"})
            elif m == "turn/completed":
                status = d["params"].get("turn", {}).get("status", "completed")
                yield BrainEvent("turn_end", {"ok": status == "completed", "text": last_message, "session_id": thread_id})
                return
        yield BrainEvent("error", {"message": f"codex exited ({await self.proc.wait()})"})


class GrokAdapter(BrainAdapter):
    id, binary = "grok", "grok"

    async def run(self, goal: str) -> AsyncIterator[BrainEvent]:
        args = ["grok", "agent", "--no-leader"]
        if self.autonomy == "bypass":
            args.append("--always-approve")
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--reasoning-effort", self.effort]
        await self._spawn(*args, "stdio")
        rid = 0

        async def req(method, params):
            nonlocal rid
            rid += 1
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

        await req("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        session_id, text = None, []
        prompt = f"{self.instructions}\n\n---\nTask from the Admiral:\n{goal}"  # ACP has no system prompt slot
        async for d in self._lines():
            m = d.get("method")
            if d.get("id") == 1 and "result" in d:
                await req("session/new", {"cwd": self.cwd, "mcpServers": []})
            elif d.get("id") == 2 and "result" in d:
                session_id = d["result"]["sessionId"]
                yield BrainEvent("ready", {"session_id": session_id})
                await req("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]})
            elif d.get("id") == 3:
                ok = "result" in d and d["result"].get("stopReason") in ("end_turn", None)
                yield BrainEvent("turn_end", {"ok": ok, "text": "".join(text), "session_id": session_id})
                return
            elif "error" in d and "id" in d and not m:
                yield BrainEvent("error", {"message": json.dumps(d["error"])[:500]})
                return
            elif m == "session/request_permission" and "id" in d:
                options = d["params"].get("options", [])
                pick = next((o for o in options if o.get("kind") == ("allow_always" if self.autonomy == "bypass" else "reject_once")), None)
                pick = pick or next((o for o in options if o.get("kind", "").startswith("allow")), options[0] if options else None)
                await self._write({"jsonrpc": "2.0", "id": d["id"],
                                   "result": {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}})
            elif m == "session/update":
                u = d["params"].get("update", {})
                kind = u.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    chunk = (u.get("content") or {}).get("text", "")
                    text.append(chunk)
                    yield BrainEvent("text_delta", {"text": chunk})
                elif kind == "tool_call":
                    text.clear()  # keep only the text after the last tool call as the final answer
                    yield BrainEvent("tool_start", {"id": u.get("toolCallId"), "name": u.get("kind") or u.get("title"),
                                                    "input": u.get("title")})
                elif kind == "tool_call_update" and u.get("status") in ("completed", "failed"):
                    yield BrainEvent("tool_end", {"id": u.get("toolCallId"), "ok": u.get("status") == "completed"})
        yield BrainEvent("error", {"message": f"grok exited ({await self.proc.wait()})"})


ADAPTERS: dict[str, type[BrainAdapter]] = {a.id: a for a in (ClaudeAdapter, CodexAdapter, GrokAdapter)}
