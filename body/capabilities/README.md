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
  "enabled": true
}
```

`effects` must list **every** tool with its effect class. The kernel rejects a capability with undeclared tools.

- `read`: only reads (status, lookups, search)
- `draft`: creates something private and reversible (a draft email, a local file)
- `act`: changes the outside world (sends, publishes, deploys, updates a ticket)

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
