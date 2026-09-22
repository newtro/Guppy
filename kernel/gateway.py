"""MCP gateway: the kernel's checkpoint between the Mind and a capability.

Mind CLIs never talk to a capability directly; the kernel registers each capability as
    python kernel/gateway.py --task <id> --cap-dir <dir> --kernel <url> -- <server command...>
This proxy starts the real server and relays MCP over stdio, but every `tools/call` is first cleared with the
kernel's gate (POST /api/gate/call), which decides by the tool's declared effect class and the task's provenance
and taint. Tools listed under "taints" in capability.json mark the task as having read external content.
If the kernel can't be reached, only `read` tools are allowed (fail closed for anything that changes the world).

Standalone script on purpose (stdlib only): it runs as a child of the Mind CLIs, not inside the kernel process.
"""
import argparse
import json
import subprocess
import sys
import threading
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--task", type=int, required=True)
ap.add_argument("--cap-dir", required=True)
ap.add_argument("--kernel", required=True)
ap.add_argument("server", nargs=argparse.REMAINDER)
a = ap.parse_args()
server_cmd = a.server[1:] if a.server and a.server[0] == "--" else a.server

manifest = json.load(open(f"{a.cap_dir}/capability.json"))
CAP, EFFECTS, TAINTS = manifest.get("name"), manifest.get("effects", {}), set(manifest.get("taints", []))
out_lock = threading.Lock()  # our stdout (to the Mind)
in_lock = threading.Lock()   # the server's stdin


def emit(msg: dict):
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def kernel(path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(a.kernel + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


proc = subprocess.Popen(server_cmd, cwd=a.cap_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
pending_taint: dict = {}  # request id -> tool name, for taint-marking once the result comes back
tainted = False           # local copy: survives the kernel being unreachable


def server_to_client():
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        tool = pending_taint.pop(msg.get("id"), None) if "id" in msg else None
        if tool and "result" in msg and not msg["result"].get("isError"):
            global tainted
            tainted = True
            try:
                kernel("/api/gate/taint", {"task_id": a.task, "capability": CAP, "tool": tool}, 10)
            except Exception:
                pass  # tainting is advisory to the gate; the gate also fails closed on act
        emit(msg)
    sys.exit(0)


def handle_call(msg: dict):
    params = msg.get("params", {})
    tool = params.get("name")
    effect = EFFECTS.get(tool, "act")  # undeclared -> treat as the most dangerous class
    try:
        verdict = kernel("/api/gate/call", {"task_id": a.task, "capability": CAP, "tool": tool, "effect": effect,
                                            "tainted": tainted, "arguments": params.get("arguments", {})}, timeout=900)
    except Exception as e:
        verdict = {"decision": "allow" if effect == "read" else "deny", "reason": f"kernel unreachable ({e})"}
    if verdict.get("decision") != "allow":
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {"isError": True, "content": [{"type": "text", "text":
              f"Blocked by Guppy's kernel: {verdict.get('reason', 'not allowed')}. Do not retry this action; "
              f"report it in your SUMMARY."}]}})
        return
    if tool in TAINTS:
        pending_taint[msg["id"]] = tool
    forward(msg)


def forward(msg: dict):
    with in_lock:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()


threading.Thread(target=server_to_client, daemon=True).start()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if msg.get("method") == "tools/call" and "id" in msg:
        threading.Thread(target=handle_call, args=(msg,), daemon=True).start()  # a held call mustn't block others
    else:
        forward(msg)
proc.terminate()
