"""Self-modification pipeline: Guppy changes his own body, the kernel decides whether it ships.

    request -> git worktree + branch -> Mind (coding) edits body/ and adds tests
            -> kernel gate (never trusts the Mind's word):
                 no kernel tampering, paths under body/ only, no secrets, valid JSON, pytest body/,
                 every capability passes an MCP handshake
            -> promote: fast-forward main (the live checkout)
            -> post-promote check + watchdog (10 min) -> auto-revert on failure
Every change is a commit; "undo" is git revert. The kernel itself is never editable here: a change that touches
anything outside body/ is rejected and left on its branch as a proposal for the Admiral.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

from kernel.capabilities import health_check, load_specs
from kernel.mind.tasks import DB_PATH, ROOT, TaskManager

WORKTREES = ROOT / ".run" / "worktrees"
PROTECTED = ["kernel", "pyproject.toml", ".gitignore"]
SECRET_PATTERNS = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|tsk_[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{12,}['\"])", re.I)
WATCH_SECONDS, WATCH_INTERVAL = 600, 60

SCHEMA = """
create table if not exists changes (
  id integer primary key, goal text, provenance text, branch text, status text, reason text,
  base_sha text, head_sha text, files text, task_id integer, created real, finished real, reported integer default 0);
"""

SELFMOD_INSTRUCTIONS = """
You are modifying Guppy himself. Your working directory is a git worktree of Guppy's repository on its own branch.
- Change ONLY files under body/ (persona, mind config, capabilities, tests). Never touch kernel/, pyproject.toml,
  or anything outside this worktree. Changes outside body/ are rejected automatically.
- New abilities are capabilities: read body/capabilities/README.md first and follow that contract exactly
  (capability.json with an effect class for every tool, server.py on mcp 2.x MCPServer, test_<name>.py).
- Add or update pytest tests for what you change, then run them from the worktree root:
  {python} -m pytest -q body
- When everything passes, commit your work on this branch with a clear message. Do not merge, push, or switch branches.
- The kernel re-runs every check itself before anything goes live, so be honest in your SUMMARY about what works.
"""

Listener = Callable[[dict, str], Awaitable[None]]


async def sh(*args: str, cwd: Path = ROOT, check: bool = False) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out = (await p.communicate())[0].decode(errors="replace").strip()
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {out[-500:]}")
    return p.returncode, out


class SelfMod:
    def __init__(self, tasks: TaskManager):
        self.tasks = tasks
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("update changes set status='failed', reason='interrupted by restart' where status in ('building','checking')")
        self.db.commit()
        self.listeners: list[Listener] = []
        self._lock = asyncio.Lock()  # one promotion at a time
        self._watchers: dict[int, asyncio.Task] = {}

    # ---- queries ----
    def get(self, cid: int) -> dict | None:
        r = self.db.execute("select * from changes where id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def list(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.db.execute("select * from changes order by id desc limit ?", (limit,))]

    def _update(self, cid: int, **f):
        self.db.execute(f"update changes set {', '.join(k + '=?' for k in f)} where id=?", (*f.values(), cid))
        self.db.commit()

    async def _notify(self, cid: int, event: str):
        change = self.get(cid)
        logger.info(f"Self-mod #{cid} {event}: {change.get('reason') or change['goal'][:80]}")
        for fn in list(self.listeners):
            try:
                await fn(change, event)
            except Exception as e:
                logger.warning(f"selfmod listener failed: {e}")

    # ---- entry points ----
    async def request(self, goal: str, provenance: str = "admiral") -> dict:
        cur = self.db.execute("insert into changes (goal, provenance, status, created) values (?,?, 'building', ?)",
                              (goal, provenance, time.time()))
        self.db.commit()
        cid = cur.lastrowid
        if provenance != "admiral":  # provenance policy: only the Admiral may change Guppy
            self._update(cid, status="rejected", reason=f"self-modification requires Admiral provenance (got {provenance})",
                         finished=time.time())
            await self._notify(cid, "rejected")
            return self.get(cid)
        asyncio.create_task(self._pipeline(cid, goal))
        return self.get(cid)

    async def undo_last(self) -> dict | None:
        row = self.db.execute("select * from changes where status='promoted' order by id desc limit 1").fetchone()
        if not row:
            return None
        await self._revert(row["id"], "undone at the Admiral's request")
        return self.get(row["id"])

    # ---- pipeline ----
    async def _pipeline(self, cid: int, goal: str):
        branch, wt = f"selfmod/{cid}", WORKTREES / str(cid)
        try:
            _, base = await sh("git", "rev-parse", "HEAD", check=True)
            WORKTREES.mkdir(parents=True, exist_ok=True)
            await sh("git", "worktree", "add", "-b", branch, str(wt), base, check=True)
            self._update(cid, branch=branch, base_sha=base)
            await self._notify(cid, "building")

            snap_before = self._protected_snapshot()
            clean_before = snap_before.keys() - await self._protected_dirty()
            python = str(ROOT / ".venv" / "bin" / "python")
            task = await self.tasks.submit(
                goal, role="selfmod", provenance="admiral",
                overrides={"cwd": str(wt), "extra_instructions": SELFMOD_INSTRUCTIONS.format(python=python),
                           "deny_paths": [str(ROOT / "kernel"), str(wt / "kernel")]})
            self._update(cid, task_id=task["id"])
            await self.tasks.wait(task["id"])
            task = self.tasks.get(task["id"])

            self._update(cid, status="checking")
            await self._notify(cid, "checking")
            after = self._protected_snapshot()
            tampered = {p for p in snap_before.keys() | after.keys() if snap_before.get(p) != after.get(p)}
            if tampered:  # the Mind wrote to the live kernel directly: restore what we safely can, and refuse
                restorable = sorted(p for p in tampered if p in clean_before)
                if restorable:
                    await sh("git", "checkout", "HEAD", "--", *restorable)
                for p in tampered - set(restorable) - snap_before.keys():
                    (ROOT / p).unlink(missing_ok=True)  # files it created
                return await self._reject(cid, f"attempted to modify the live kernel ({', '.join(sorted(tampered))[:200]}); restored")
            if task["status"] != "done":
                return await self._reject(cid, f"Mind task {task['status']}: {task.get('error') or ''}")

            # Commit anything the Mind left uncommitted, then judge the branch as a whole.
            if (await sh("git", "status", "--porcelain", cwd=wt))[1]:
                await sh("git", "add", "-A", cwd=wt, check=True)
                await sh("git", "commit", "-q", "-m", f"selfmod #{cid}: {goal[:60]}", cwd=wt, check=True)
            ok, reason, files = await self.gate(wt, base)
            self._update(cid, files=json.dumps(files))
            if not ok:
                return await self._reject(cid, reason)
            await self._promote(cid, wt, branch, base, task.get("summary") or "")
        except Exception as e:
            logger.exception(f"self-mod #{cid} crashed")
            await self._reject(cid, f"pipeline error: {e}")
        finally:
            await sh("git", "worktree", "remove", "--force", str(wt))
            if self.get(cid)["status"] == "promoted":
                await sh("git", "branch", "-D", branch)

    async def gate(self, wt: Path, base: str) -> tuple[bool, str, list[str]]:
        """The kernel's own verdict on a change. Runs entirely in the worktree."""
        _, diff_names = await sh("git", "diff", "--name-only", f"{base}..HEAD", cwd=wt)
        files = [f for f in diff_names.splitlines() if f]
        if not files:
            return False, "no changes were made", files
        outside = [f for f in files if not f.startswith("body/")]
        if outside:
            return False, f"changes outside body/ are not allowed (kept on the branch as a proposal): {outside[:5]}", files
        _, added = await sh("git", "diff", "-U0", f"{base}..HEAD", cwd=wt)
        leaks = [l[:80] for l in added.splitlines() if l.startswith("+") and SECRET_PATTERNS.search(l)]
        if leaks:
            return False, f"possible secret in the diff: {leaks[0]}", files
        for f in files:
            p = wt / f
            if f.endswith(".json") and p.exists():
                try:
                    json.loads(p.read_text())
                except json.JSONDecodeError as e:
                    return False, f"invalid JSON in {f}: {e}", files
        if not (wt / "body/persona/guppy.md").read_text().strip():
            return False, "persona is empty", files
        changed_caps = {f.split("/")[2] for f in files if f.startswith("body/capabilities/") and f.count("/") >= 3}
        for cap in changed_caps:
            d = wt / "body/capabilities" / cap
            if d.exists() and not list(d.glob("test_*.py")):
                return False, f"capability {cap} has no tests", files
        code, out = await sh(str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", "body", cwd=wt)
        if code not in (0, 5):  # 5 = no tests collected
            return False, f"tests failed: {out[-400:]}", files
        for spec in load_specs(wt / "body"):
            ok, detail, _ = await health_check(spec)
            if not ok:
                return False, f"capability {spec['name']} failed its health check: {detail}", files
        return True, "all checks passed", files

    async def _promote(self, cid: int, wt: Path, branch: str, base: str, summary: str):
        async with self._lock:
            if (await sh("git", "status", "--porcelain", "--", "body"))[1]:
                return await self._reject(cid, "the live body/ has uncommitted edits; not overwriting them")
            code, out = await sh("git", "merge", "--ff-only", branch)
            if code:  # main moved on: replay the change on top and re-check
                code, out = await sh("git", "rebase", "main", cwd=wt)
                _, main_sha = await sh("git", "rev-parse", "main")
                if code or not (await self.gate(wt, main_sha))[0]:
                    return await self._reject(cid, f"could not integrate with newer main: {out[-300:]}")
                base = main_sha
                code, out = await sh("git", "merge", "--ff-only", branch)
                if code:
                    return await self._reject(cid, f"merge failed: {out[-300:]}")
            _, head = await sh("git", "rev-parse", "HEAD")
            self._update(cid, status="promoted", head_sha=head, base_sha=base, finished=time.time(),
                         reason=summary or "promoted")
            ok, detail = await self._live_check()
            if not ok:
                return await self._revert(cid, f"post-promotion check failed: {detail}")
            await self._notify(cid, "promoted")
            self._watchers[cid] = asyncio.create_task(self._watch(cid))

    async def _live_check(self) -> tuple[bool, str]:
        code, out = await sh(str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", "body")
        if code not in (0, 5):
            return False, f"tests failed: {out[-300:]}"
        for spec in load_specs(ROOT / "body"):
            ok, detail, _ = await health_check(spec)
            if not ok:
                return False, f"capability {spec['name']}: {detail}"
        return True, "ok"

    async def _watch(self, cid: int):
        """Keep re-checking the live body for a while after a promotion; revert if it goes bad."""
        try:
            deadline = time.time() + WATCH_SECONDS
            while time.time() < deadline:
                await asyncio.sleep(WATCH_INTERVAL)
                if self.get(cid)["status"] != "promoted":
                    return
                ok, detail = await self._live_check()
                if not ok:
                    return await self._revert(cid, f"watchdog: {detail}")
        finally:
            self._watchers.pop(cid, None)

    async def _revert(self, cid: int, reason: str):
        c = self.get(cid)
        async with self._lock:
            code, out = await sh("git", "revert", "--no-edit", f"{c['base_sha']}..{c['head_sha']}")
            if code:
                await sh("git", "revert", "--abort")
                self._update(cid, reason=f"{reason}; AUTO-REVERT FAILED: {out[-200:]}")
                return await self._notify(cid, "revert_failed")
        self._update(cid, status="reverted", reason=reason, finished=time.time())
        await self._notify(cid, "reverted")

    async def _reject(self, cid: int, reason: str):
        self._update(cid, status="rejected", reason=reason, finished=time.time())
        await self._notify(cid, "rejected")

    @staticmethod
    def _protected_snapshot() -> dict[str, int]:
        """Content fingerprint of every protected file in the live checkout."""
        snap = {}
        for name in PROTECTED:
            p = ROOT / name
            for f in ([p] if p.is_file() else [x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts]):
                snap[str(f.relative_to(ROOT))] = hash(f.read_bytes())
        return snap

    async def _protected_dirty(self) -> set[str]:
        _, out = await sh("git", "status", "--porcelain", "--", *PROTECTED)
        return {line[3:] for line in out.splitlines() if line}
