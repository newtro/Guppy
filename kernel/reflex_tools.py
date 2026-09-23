"""Instant capability tools: Guppy-built tools the reflex (voice) LLM may call directly, mid-conversation.

A capability opts tools in with "reflex": ["tool", ...] in capability.json. Only effect classes `read` and `draft`
qualify (nothing that changes the outside world runs without the Mind and the gate). The kernel keeps each such
capability's MCP server running, lists its tools, and exposes them to the reflex LLM as
`<capability>__<tool>`. A tool may return a `display` card ({"title": ..., "lines": [...]}) that the kernel shows
on screen next to Guppy's head.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger

from kernel.capabilities import load_specs

REFLEX_EFFECTS = ("read", "draft")


class _McpServer:
    """A long-lived stdio MCP server with a minimal JSON-RPC client."""

    def __init__(self, spec: dict):
        self.spec, self.proc, self._id, self._lock = spec, None, 0, asyncio.Lock()

    async def start(self) -> list[dict]:
        self.proc = await asyncio.create_subprocess_exec(
            self.spec["command"], *self.spec["args"], cwd=self.spec["cwd"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024)
        await self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "guppy-reflex", "version": "0.1"}})
        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        return (await self._rpc("tools/list", {})).get("tools", [])

    async def _rpc(self, method: str, params: dict, timeout: float = 20) -> dict:
        async with self._lock:
            self._id += 1
            rid = self._id
            self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
            await self.proc.stdin.drain()
            async with asyncio.timeout(timeout):
                while True:
                    line = await self.proc.stdout.readline()
                    if not line:
                        raise RuntimeError(f"{self.spec['name']} exited")
                    msg = json.loads(line)
                    if msg.get("id") == rid:
                        if "error" in msg:
                            raise RuntimeError(msg["error"].get("message", "tool error"))
                        return msg.get("result", {})

    async def call(self, tool: str, args: dict) -> dict:
        return await self._rpc("tools/call", {"name": tool, "arguments": args})

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


class ReflexToolHost:
    def __init__(self, body: Path):
        self.body = body
        self.servers: dict[str, _McpServer] = {}
        self.tools: dict[str, dict] = {}          # exposed name -> {"cap", "tool", "description", "schema"}
        self._stamp: dict[str, float] = {}        # capability -> newest file mtime (restart on change)

    @staticmethod
    def _mtime(d: Path) -> float:
        return max((f.stat().st_mtime for f in d.rglob("*") if f.is_file() and "__pycache__" not in f.parts), default=0)

    async def refresh(self):
        """(Re)start capabilities whose files changed; drop removed ones. Called at each session start."""
        wanted = {}
        for spec in load_specs(self.body):
            manifest = json.loads((Path(spec["cwd"]) / "capability.json").read_text())
            reflex = [t for t in manifest.get("reflex", []) if spec["effects"].get(t) in REFLEX_EFFECTS]
            if reflex:
                wanted[spec["name"]] = (spec, set(reflex))
        for name in list(self.servers):
            if name not in wanted or self._mtime(Path(wanted[name][0]["cwd"])) != self._stamp.get(name):
                await self.servers.pop(name).stop()
                self.tools = {k: v for k, v in self.tools.items() if v["cap"] != name}
        for name, (spec, allowed) in wanted.items():
            if name in self.servers:
                continue
            server = _McpServer(spec)
            try:
                listed = await server.start()
            except Exception as e:
                logger.warning(f"Instant tools from {name} unavailable: {e}")
                await server.stop()
                continue
            self.servers[name], self._stamp[name] = server, self._mtime(Path(spec["cwd"]))
            for t in listed:
                if t["name"] in allowed:
                    self.tools[f"{name}__{t['name']}"] = {"cap": name, "tool": t["name"], "description": t.get("description", ""),
                                                          "schema": t.get("inputSchema") or {"type": "object", "properties": {}}}
        if self.tools:
            logger.info(f"Instant tools for the reflex: {', '.join(self.tools)}")

    def function_schemas(self):
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        return [FunctionSchema(name=n, description=f"[{t['cap']} capability] {t['description']}",
                               properties=t["schema"].get("properties", {}), required=t["schema"].get("required", []))
                for n, t in self.tools.items()]

    async def call(self, name: str, args: dict) -> dict:
        """Returns {"result": ..., "display": {...}?}."""
        t = self.tools[name]
        res = await self.servers[t["cap"]].call(t["tool"], args)
        payload = res.get("structuredContent")
        if payload is None:
            text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                payload = text
        if isinstance(payload, dict) and set(payload) == {"result"} and isinstance(payload["result"], dict):
            payload = payload["result"]  # mcp wraps non-object returns as {"result": ...}
        out = {"result": payload}
        if isinstance(payload, dict) and isinstance(payload.get("display"), dict):
            out["display"] = payload["display"]
        if res.get("isError"):
            out["error"] = True
        return out
