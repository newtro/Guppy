"""The provenance gate: decides every capability tool call the Mind makes.

    effect  | clean task (Admiral-only inputs)       | tainted task (read external content) / external provenance
    --------+----------------------------------------+------------------------------------------------------------
    read    | allow                                  | allow
    draft   | allow                                  | allow
    act     | allow after a short hold (cancellable) | hold until the Admiral confirms out loud; deny on timeout

A task becomes tainted when a capability tool listed under "taints" returns content (e.g. reading an email body).
Taint never clears for that task. Every decision is written to the `actions` table.
Confirmation is deliberately NOT exposed over HTTP: only the Admiral's voice (the reflex tool) or the kernel
itself can confirm, so a Mind with a shell cannot approve its own actions.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable

from loguru import logger

from kernel.mind.tasks import DB_PATH, TaskManager

SCHEMA = """
create table if not exists actions (
  id integer primary key, task_id integer, capability text, tool text, effect text, arguments text,
  tainted integer, status text, reason text, created real, decided real);
"""
Listener = Callable[[dict, str], Awaitable[None]]


def describe(action: dict) -> str:
    """One spoken-friendly line about an action."""
    try:
        args = json.loads(action.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    bits = [f"{k} {str(v)[:60]}" for k, v in args.items() if k in ("to", "subject", "title", "url", "id", "name", "path")]
    return f"{action['capability']} {action['tool'].replace('_', ' ')}" + (f" ({'; '.join(bits)})" if bits else "")


class Gate:
    def __init__(self, tasks: TaskManager, config: Callable[[], dict]):
        self.tasks, self.config = tasks, config
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("update actions set status='denied', reason='kernel restarted' where status in ('holding','awaiting_confirmation')")
        self.db.commit()
        self.tainted: set[int] = set()
        self.waiters: dict[int, asyncio.Future] = {}
        self.listeners: list[Listener] = []

    def get(self, aid: int) -> dict | None:
        r = self.db.execute("select * from actions where id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def list(self, limit: int = 20, pending_only: bool = False) -> list[dict]:
        q = "select * from actions" + (" where status in ('holding','awaiting_confirmation')" if pending_only else "") + " order by id desc limit ?"
        return [dict(r) for r in self.db.execute(q, (limit,))]

    def _update(self, aid: int, **f):
        self.db.execute(f"update actions set {', '.join(k + '=?' for k in f)} where id=?", (*f.values(), aid))
        self.db.commit()

    async def _notify(self, aid: int, event: str):
        a = self.get(aid)
        logger.info(f"Gate action #{aid} {event}: {describe(a)} (task {a['task_id']})")
        for fn in list(self.listeners):
            try:
                await fn(a, event)
            except Exception as e:
                logger.warning(f"gate listener failed: {e}")

    # ---- called by the gateway (via HTTP) ----
    def taint(self, task_id: int, capability: str, tool: str):
        if task_id not in self.tainted:
            logger.info(f"Task #{task_id} tainted by {capability}.{tool} (read external content)")
        self.tainted.add(task_id)

    async def decide(self, task_id: int, capability: str, tool: str, effect: str, arguments: dict,
                     gateway_tainted: bool = False) -> dict:
        task = self.tasks.get(task_id)
        if not task or task["status"] not in ("queued", "running"):
            return {"decision": "deny", "reason": "unknown or finished task"}
        tainted = gateway_tainted or task_id in self.tainted or task["provenance"] != "admiral"
        if gateway_tainted:
            self.tainted.add(task_id)
        cur = self.db.execute(
            "insert into actions (task_id, capability, tool, effect, arguments, tainted, status, created) values (?,?,?,?,?,?,?,?)",
            (task_id, capability, tool, effect, json.dumps(arguments, default=str)[:4000], int(tainted), "deciding", time.time()))
        self.db.commit()
        aid = cur.lastrowid
        if effect in ("read", "draft"):
            self._update(aid, status="allowed", reason=f"{effect} is always allowed", decided=time.time())
            return {"decision": "allow"}

        cfg = self.config().get("gate", {})
        fut = asyncio.get_running_loop().create_future()
        self.waiters[aid] = fut
        try:
            if not tainted:  # Admiral's own request: short hold so a misheard command can still be cancelled
                self._update(aid, status="holding")
                await self._notify(aid, "holding")
                try:
                    verdict = await asyncio.wait_for(asyncio.shield(fut), cfg.get("act_hold_s", 15))
                except TimeoutError:
                    verdict = ("allowed", "hold window passed")
            else:  # the task has read external content: the Admiral decides
                self._update(aid, status="awaiting_confirmation")
                await self._notify(aid, "awaiting_confirmation")
                try:
                    verdict = await asyncio.wait_for(asyncio.shield(fut), cfg.get("confirm_timeout_s", 300))
                except TimeoutError:
                    verdict = ("denied", "no confirmation from the Admiral")
        finally:
            self.waiters.pop(aid, None)
        status, reason = verdict
        self._update(aid, status=status, reason=reason, decided=time.time())
        await self._notify(aid, status)
        return {"decision": "allow" if status == "allowed" else "deny", "reason": reason}

    # ---- called only in-process (Admiral's voice / kernel) ----
    def resolve(self, aid: int, allow: bool, reason: str) -> bool:
        fut = self.waiters.get(aid)
        if not fut or fut.done():
            return False
        fut.set_result(("allowed" if allow else "denied", reason))
        return True

    def latest_pending(self) -> dict | None:
        rows = self.list(limit=1, pending_only=True)
        return rows[0] if rows else None
