# Capabilities

Each capability is a small MCP server that gives Guppy's Mind new tools. The kernel discovers every folder
here, health-checks it, and hands the healthy ones to every Mind task.

## Layout

```
body/capabilities/<name>/
  capability.json   # manifest (required)
  server.py         # MCP server over stdio (required)
  test_<name>.py    # pytest tests for the tool logic (required)
```

## capability.json

```json
{
  "name": "clock",
  "description": "One line: what this gives Guppy.",
  "effects": { "now": "read" },
  "taints": [],
  "reflex": ["now"],
  "enabled": true
}
```

`effects` must list **every** tool with its effect class. The kernel rejects a capability with undeclared tools.

- `read`: only reads (status, lookups, search)
- `draft`: creates something private and reversible (a draft email, a local file)
- `act`: changes the outside world (sends, publishes, deploys, updates a ticket)

`taints` lists the tools that return **external content** someone else wrote (email bodies, web pages, ticket
text, comments). Once a task calls one, the task is tainted for good: its `act` calls then need the Admiral's
spoken confirmation. When in doubt, list it.

## Instant tools (optional)

List tools under `"reflex"` to let Guppy's voice model call them directly, mid-conversation, with no Mind task
(fast: no agent startup). Only `read` and `draft` tools qualify; `act` tools are ignored there. Keep them quick
(well under a second) and side-effect free apart from showing something.

A tool can put a card on screen next to Guppy's head by returning a dict with a `display` key:

```python
return {"result": 6, "display": {"title": "Calculator", "lines": ["3 + 3", "= 6"], "seconds": 12}}
```

The kernel strips `display` before the voice model sees the result. Lines render top to bottom; the last one is
the emphasized result.

## The gate (enforced by the kernel, not by you)

Every tool call goes through the kernel's gateway. `read` and `draft` always run. `act` runs after a short
cancellable hold on a clean task, and needs the Admiral's spoken "yes" on a tainted task. A blocked call returns
an error that starts with "Blocked by Guppy's kernel". Don't try to route around it; report it.

## server.py (mcp 2.x, which renamed FastMCP to MCPServer)

```python
from datetime import datetime
from mcp.server.mcpserver import MCPServer

server = MCPServer("clock")

@server.tool()
def now() -> str:
    """Current local date and time."""  # the docstring is the tool description the Mind sees
    return datetime.now().isoformat(timespec="seconds")

if __name__ == "__main__":
    server.run()  # stdio
```

## Rules

- Runs with Guppy's Python venv. Only the standard library, `mcp`, `httpx`, and `numpy` are available. You
  cannot add dependencies (pyproject.toml belongs to the kernel). Shelling out to macOS tools is fine.
- Never print to stdout except through MCP (stdout is the protocol). Log to stderr.
- Secrets come from environment variables or the macOS Keychain at runtime. Never write secrets into files.
- Keep tools small and typed. Return plain strings or JSON-able dicts.
- Put the testable logic in plain functions and test those in `test_<name>.py` (runs with pytest from repo root).
