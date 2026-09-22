"""Guppy's Mind task manager: queue, run, persist, and report background tasks.

Tasks run on the provider chosen by role (body/mind/config.json), fall back to the next provider if one
fails before doing any work, and are persisted (with every tool call, as an audit trail) in SQLite.
Listeners (the voice loop, the HUD) get a callback on every task state change.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

from kernel.capabilities import healthy_specs
from kernel.mind.adapters import ADAPTERS, BrainEvent

ROOT = Path(__file__).resolve().parents[2]
BODY = ROOT / "body"
DB_PATH = ROOT / ".run" / "guppy.db"

SCHEMA = """
create table if not exists tasks (
  id integer primary key, goal text not null, role text, provider text, provenance text,
  status text not null, summary text, result text, error text, session_id text, cost_usd real,
  created real, started real, finished real, reported integer default 0);
create table if not exists task_events (
  task_id integer, ts real, type text, data text);
"""

Listener = Callable[[dict, str], Awaitable[None]]


def extract_summary(text: str) -> str:
    """The spoken part of a Mind report: everything after the last 'SUMMARY:' line."""
    i = text.rfind("SUMMARY:")
    s = text[i + len("SUMMARY:"):] if i >= 0 else text[-400:]
    return " ".join(s.split())[:600]


class TaskManager:
    def __init__(self):
        DB_PATH.parent.mkdir(exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Anything left running by a previous process died with it.
        self.db.execute("update tasks set status='failed', error='interrupted by restart' where status in ('queued','running')")
        self.db.commit()
        self.listeners: list[Listener] = []
        self.running: dict[int, asyncio.Task] = {}
        self.adapters: dict[int, object] = {}
        self.overrides: dict[int, dict] = {}  # per-task: cwd, extra_instructions, deny_paths
        self._sem: asyncio.Semaphore | None = None

    @property
    def config(self) -> dict:
        return json.loads((BODY / "mind" / "config.json").read_text())  # re-read: Guppy may tune it

    def instructions(self) -> str:
        return (BODY / "mind" / "instructions.md").read_text()

    # ---- queries ----
    def get(self, task_id: int) -> dict | None:
        row = self.db.execute("select * from tasks where id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 20, active_only: bool = False) -> list[dict]:
        q = "select * from tasks" + (" where status in ('queued','running')" if active_only else "") + " order by id desc limit ?"
        return [dict(r) for r in self.db.execute(q, (limit,))]

    def unreported(self) -> list[dict]:
        q = "select * from tasks where status in ('done','failed') and reported=0 order by id"
        return [dict(r) for r in self.db.execute(q)]

    def mark_reported(self, task_id: int):
        self._update(task_id, reported=1)

    def events(self, task_id: int) -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from task_events where task_id=? order by ts", (task_id,))]

    # ---- commands ----
    async def wait(self, task_id: int):
        if (t := self.running.get(task_id)):
            await asyncio.shield(t)

    async def submit(self, goal: str, role: str = "general", provenance: str = "admiral", provider: str | None = None,
                     overrides: dict | None = None) -> dict:
        cfg = self.config
        provider = provider or cfg["roles"].get(role, cfg["default_provider"])
        cur = self.db.execute(
            "insert into tasks (goal, role, provider, provenance, status, created) values (?,?,?,?, 'queued', ?)",
            (goal, role, provider, provenance, time.time()))
        self.db.commit()
        task = self.get(cur.lastrowid)
        self.overrides[task["id"]] = overrides or {}
        logger.info(f"Mind task #{task['id']} queued ({role} -> {provider}): {goal[:80]}")
        await self._notify(task, "queued")
        self.running[task["id"]] = asyncio.create_task(self._run(task["id"]))
        return task

    async def cancel(self, task_id: int) -> bool:
        t = self.running.get(task_id)
        if not t:
            return False
        t.cancel()
        return True

    # ---- internals ----
    def _update(self, task_id: int, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"update tasks set {cols} where id=?", (*fields.values(), task_id))
        self.db.commit()

    def _log(self, task_id: int, e: BrainEvent):
        if e.type == "text_delta":
            return  # final text is stored on the task; deltas would bloat the audit log
        self.db.execute("insert into task_events values (?,?,?,?)", (task_id, time.time(), e.type, json.dumps(e.data, default=str)[:4000]))
        self.db.commit()

    async def _notify(self, task: dict, event: str):
        for fn in list(self.listeners):
            try:
                await fn(task, event)
            except Exception as e:  # a broken listener must not kill the task
                logger.warning(f"task listener failed: {e}")

    async def _run(self, task_id: int):
        cfg = self.config
        self._sem = self._sem or asyncio.Semaphore(cfg.get("max_concurrent_tasks", 3))
        task = self.get(task_id)
        ov = self.overrides.pop(task_id, {})
        instructions = self.instructions() + ("\n" + ov["extra_instructions"] if ov.get("extra_instructions") else "")
        order = [task["provider"]] + [p for p in cfg["fallback_order"] if p != task["provider"]]
        order = [p for p in order if p in ADAPTERS and ADAPTERS[p].available()]
        try:
            async with self._sem:
                self._update(task_id, status="running", started=time.time())
                await self._notify(self.get(task_id), "running")
                last_error = "no provider available"
                capabilities = await healthy_specs(BODY)
                for provider in order:
                    pcfg = cfg["providers"].get(provider, {})
                    adapter = ADAPTERS[provider](cwd=ov.get("cwd", cfg["workdir"]), instructions=instructions,
                                                 model=pcfg.get("model"), effort=pcfg.get("effort"), autonomy=cfg["autonomy"],
                                                 mcp_servers=capabilities, deny_paths=ov.get("deny_paths"))
                    self.adapters[task_id] = adapter
                    self._update(task_id, provider=provider)
                    did_work, end = False, None
                    try:
                        async with asyncio.timeout(cfg.get("task_timeout_s", 1800)):
                            async for e in adapter.run(task["goal"]):
                                self._log(task_id, e)
                                if e.type == "ready":
                                    self._update(task_id, session_id=e.data.get("session_id"))
                                elif e.type == "tool_start":
                                    did_work = True
                                    await self._notify({**self.get(task_id), "tool": e.data.get("name")}, "progress")
                                elif e.type in ("turn_end", "error"):
                                    end = e
                                    break
                    except TimeoutError:
                        end = BrainEvent("error", {"message": f"timed out after {cfg.get('task_timeout_s')}s"})
                    finally:
                        await adapter.close()
                    if end and end.type == "turn_end" and end.data.get("ok"):
                        text = end.data.get("text", "")
                        self._update(task_id, status="done", result=text, summary=extract_summary(text),
                                     cost_usd=end.data.get("cost_usd"), finished=time.time())
                        break
                    last_error = (end.data.get("message") if end and end.type == "error" else "provider reported failure") if end else "provider exited"
                    if did_work:
                        break  # it acted in the world; don't silently redo the task elsewhere
                    logger.warning(f"Mind task #{task_id}: {provider} failed ({last_error}); trying next provider")
                else:
                    pass
                if self.get(task_id)["status"] != "done":
                    self._update(task_id, status="failed", error=last_error, finished=time.time())
        except asyncio.CancelledError:
            if (a := self.adapters.get(task_id)):
                await a.close()
            self._update(task_id, status="cancelled", finished=time.time())
        finally:
            self.running.pop(task_id, None)
            self.adapters.pop(task_id, None)
            final = self.get(task_id)
            logger.info(f"Mind task #{task_id} {final['status']}: {final.get('summary') or final.get('error')}")
            await self._notify(final, final["status"])
