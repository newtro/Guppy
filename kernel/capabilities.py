"""Capability registry: Guppy's self-built features, each an MCP server under body/capabilities/<name>/.

The kernel discovers them, health-checks them (MCP handshake + tools/list), and hands the healthy ones to
every Mind task, so a capability Guppy builds for himself is usable by his next task. Contract:
body/capabilities/README.md.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable  # capabilities run in Guppy's venv


def load_specs(body: Path) -> list[dict]:
    """Enabled capabilities under <body>/capabilities, resolved to absolute stdio commands."""
    specs = []
    for manifest in sorted((body / "capabilities").glob("*/capability.json")):
        try:
            m = json.loads(manifest.read_text())
        except json.JSONDecodeError as e:
            logger.warning(f"capability {manifest.parent.name}: bad capability.json ({e})")
            continue
        if not m.get("enabled", True):
            continue
        d = manifest.parent.resolve()
        entry = d / m.get("entry", "server.py")
        specs.append({"name": m.get("name", d.name), "description": m.get("description", ""),
                      "command": PYTHON, "args": [str(entry)], "cwd": str(d), "effects": m.get("effects", {})})
    return specs


async def health_check(spec: dict, timeout: float = 30) -> tuple[bool, str, list[str]]:
    """Start the server, do the MCP handshake, list tools. Returns (ok, detail, tool_names)."""
    proc = await asyncio.create_subprocess_exec(
        spec["command"], *spec["args"], cwd=spec["cwd"], stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode()); await proc.stdin.drain()

    async def recv(want_id):
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("server exited: " + (await proc.stderr.read()).decode()[-400:])
            msg = json.loads(line)
            if msg.get("id") == want_id:
                return msg

    try:
        async with asyncio.timeout(timeout):
            await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "guppy-kernel", "version": "0.1"}}})
            init = await recv(1)
            if "error" in init:
                return False, f"initialize failed: {init['error']}", []
            await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = [t["name"] for t in (await recv(2)).get("result", {}).get("tools", [])]
            if not tools:
                return False, "server exposes no tools", []
            undeclared = [t for t in tools if t not in spec.get("effects", {})]
            if undeclared:
                return False, f"tools missing an effect class in capability.json: {undeclared}", tools
            return True, f"{len(tools)} tools", tools
    except (TimeoutError, RuntimeError, json.JSONDecodeError) as e:
        return False, f"handshake failed: {e}", []
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def gated(specs: list[dict], task_id: int, kernel_url: str) -> list[dict]:
    """Route each capability through the kernel gateway for this task (see kernel/gateway.py)."""
    gw = str(Path(__file__).parent / "gateway.py")
    return [{**s, "args": [gw, "--task", str(task_id), "--cap-dir", s["cwd"], "--kernel", kernel_url, "--",
                           s["command"], *s["args"]]} for s in specs]


async def healthy_specs(body: Path) -> list[dict]:
    specs = load_specs(body)
    results = await asyncio.gather(*(health_check(s) for s in specs))
    out = []
    for spec, (ok, detail, _) in zip(specs, results):
        if ok:
            out.append(spec)
        else:
            logger.warning(f"capability {spec['name']} unhealthy, not loaded: {detail}")
    return out
