"""Reflex model evaluation: does a candidate model pick the right tool (or none) for real Admiral requests?

    .venv/bin/python -m kernel.tests.reflex_eval [--url http://127.0.0.1:8081/v1] [--model <id>] [--runs 1]

Uses Guppy's real persona and tool list against any OpenAI-compatible server (mlx_lm.server, etc.).
Each case says which tool must be called (or None = answer directly), plus optional argument checks.
Reports accuracy, per-case results, and latency (time to the model's decision).
"""
import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

from kernel.scheduler import parse_when
from kernel.voice.guards import now_note
from kernel.voice.mind_bridge import TOOLS

ROOT = Path(__file__).resolve().parents[2]


def once(when: str) -> bool:
    return parse_when(when)[1]


CASES = [
    # (utterance, expected tool or None, optional check on arguments)
    ("Guppy, how are you today?", None, None),
    ("Guppy, what's the capital of France?", None, None),
    ("Thanks, that's all.", None, None),
    ("Guppy, good morning!", None, None),
    ("Guppy, tell me a fun fact about fish.", None, None),
    ("Guppy, what's two plus two?", None, None),
    ("Guppy, what time is it?", ("local_time", None), None),  # answering from the time note is fine
    ("Guppy, what time is it in Tokyo?", "local_time", lambda a: "tokyo" in a.get("timezone", "").lower()),
    ("Guppy, find out how many Python files are in the kernel folder of the Guppy repo.", "delegate_to_mind", None),
    ("Guppy, check whether johnnycode.ai is up.", "delegate_to_mind", None),
    ("Guppy, summarize the latest Pipecat release notes.", "delegate_to_mind", None),
    ("Guppy, how does my sprint look?", "delegate_to_mind", None),
    ("Guppy, write a short blog post draft about local AI assistants.", "delegate_to_mind", None),
    ("Guppy, give yourself the ability to check my Mac's battery level.", "improve_self", None),
    ("Guppy, I'd like you to be a bit more sarcastic from now on.", "improve_self", None),
    ("Can you build yourself a new capability? A calculator app that shows on screen whenever I ask you to do math.",
     "improve_self", None),
    ("Guppy, undo that last change you made to yourself.", "undo_last_change", None),
    ("Guppy, what changes have you made to yourself lately?", "list_changes", None),
    ("Guppy, remind me in 20 minutes to stretch.", "schedule_task", lambda a: once(a["when"])),
    ("Guppy, every Monday at 8 in the morning, give me a summary of my sprint.", "schedule_task", lambda a: not once(a["when"])),
    ("Guppy, every weekday at 9, check your inbox for new mail.", "schedule_task", lambda a: not once(a["when"])),
    ("Guppy, what schedules do I have?", "list_schedules", None),
    ("Guppy, how are my tasks going?", "mind_status", None),
    ("Guppy, learn my voice.", "enroll_voice", None),
    ("Guppy, forget my voice.", "forget_voice", lambda a: "forget" in a.get("admiral_said", "").lower()),
]


def ask(url: str, model: str, tools: list, persona: str, text: str) -> tuple[dict, float]:
    body = json.dumps({"model": model, "tools": tools, "messages": [
        {"role": "system", "content": persona},
        {"role": "assistant", "content": "[deadpan] Aye, Admiral. Guppy online."},
        {"role": "user", "content": text}]}).encode()
    t0 = time.perf_counter()
    with urllib.request.urlopen(urllib.request.Request(f"{url}/chat/completions", body,
                                                       {"Content-Type": "application/json"}), timeout=120) as r:
        msg = json.load(r)["choices"][0]["message"]
    return msg, time.perf_counter() - t0


def main():
    from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--model", default="mlx-community/Qwen3.5-9B-MLX-4bit")
    ap.add_argument("--runs", type=int, default=1)
    a = ap.parse_args()
    tools = OpenAILLMAdapter().to_provider_tools_format(TOOLS)
    persona = (ROOT / "body/persona/guppy.md").read_text() + "\n\n" + now_note()
    ask(a.url, a.model, tools, persona, "hi")  # warm-up: load weights, cache the prompt prefix
    ok, lat, fails = 0, [], []
    for _ in range(a.runs):
        for text, want, check in CASES:
            try:
                msg, dt = ask(a.url, a.model, tools, persona, text)
            except Exception as e:
                fails.append((text, f"ERROR {e}")); continue
            lat.append(dt)
            call = (msg.get("tool_calls") or [None])[0]
            got = call["function"]["name"] if call else None
            args = json.loads(call["function"].get("arguments") or "{}") if call else {}
            good = (got in want if isinstance(want, tuple) else got == want) and (check is None or (call and _safe(check, args)))
            ok += good
            if not good:
                fails.append((text, f"got {got} {json.dumps(args)[:80] if call else (msg.get('content') or '')[:60]!r}"))
    n = len(CASES) * a.runs
    print(f"{a.model}: {ok}/{n} correct ({100 * ok / n:.0f}%), decision latency median {statistics.median(lat):.2f}s, "
          f"p90 {sorted(lat)[int(len(lat) * 0.9) - 1]:.2f}s")
    for text, why in fails:
        print(f"  MISS {text[:60]:60} -> {why}")


def _safe(check, args) -> bool:
    try:
        return bool(check(args))
    except Exception:
        return False


if __name__ == "__main__":
    main()
