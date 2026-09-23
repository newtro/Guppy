"""Bridge between the Reflex (voice pipeline) and the Mind (background tasks).

- Gives the reflex LLM tools: delegate_to_mind, mind_status, cancel_mind_task.
- Streams task updates to the client HUD.
- When a task finishes, injects a "[Mind report]" into the conversation so Guppy says it out loud.
  Reports that finish while nobody is connected are delivered on the next connection.
"""
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.llm_service import FunctionCallParams

from kernel.gate import Gate, describe
from kernel.mind.tasks import TaskManager
from kernel.scheduler import Scheduler, describe as describe_schedule
from kernel.selfmod import SelfMod

TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema(
        name="delegate_to_mind",
        description="Hand a task to the Mind, Guppy's capable background agent with shell, files, web and code tools. "
                    "Use for anything needing tools, current information, or real work.",
        properties={
            "goal": {"type": "string", "description": "The task, complete and self-contained (the Mind cannot hear the conversation)."},
            "role": {"type": "string", "enum": ["general", "coding", "research", "review"],
                     "description": "coding: code/repos; research: web research; review: second opinion; general: anything else."},
        },
        required=["goal"],
    ),
    FunctionSchema(
        name="mind_status",
        description="List the Mind's active and recent tasks with their status and summaries.",
        properties={}, required=[],
    ),
    FunctionSchema(
        name="improve_self",
        description="Change Guppy himself: add a new ability (a capability/tool), change his personality or behavior, "
                    "or fix one of his own features. The kernel tests the change and only ships it if every check passes.",
        properties={"goal": {"type": "string", "description": "What to change or add, complete and self-contained."}},
        required=["goal"],
    ),
    FunctionSchema(
        name="undo_last_change",
        description="Revert the most recent self-modification that went live.",
        properties={}, required=[],
    ),
    FunctionSchema(
        name="list_changes",
        description="List recent self-modifications and whether they shipped.",
        properties={}, required=[],
    ),
    FunctionSchema(
        name="confirm_action",
        description="The Admiral approves a pending Mind action (say yes to an '[Action request]'). "
                    "Only call this when the Admiral clearly says yes, confirm, approve, or go ahead.",
        properties={"action_id": {"type": "integer", "description": "The action number; omit for the latest pending one."}},
        required=[],
    ),
    FunctionSchema(
        name="cancel_action",
        description="Stop a pending Mind action (an '[Action request]' or an action in its hold window).",
        properties={"action_id": {"type": "integer", "description": "The action number; omit for the latest pending one."}},
        required=[],
    ),
    FunctionSchema(
        name="schedule_task",
        description="Schedule a Mind task to run later or on a repeating schedule (reminders, digests, recurring checks). "
                    "When it runs, the result is reported to the Admiral like any Mind task.",
        properties={
            "goal": {"type": "string", "description": "What the Mind should do each time, complete and self-contained. "
                                                      "For a reminder, e.g. 'Remind the Admiral to call the dentist.'"},
            "when": {"type": "string", "description": "RECURRING: 'every monday at 8:00', 'every weekday at 9am', "
                     "'every day at 17:30', 'every mon and thu at 7:15 pm', 'every month on the 1st at 9:00', 'every 30m', 'every 6h'. "
                     "ONE-TIME: 'in 20m', 'in 2h', 'once at 14:30'. Use a recurring form whenever the Admiral says "
                     "every/each/daily/weekly. Advanced: a 5-field cron like '0 9 1 * *' (9:00 on the 1st of each month)."},
            "role": {"type": "string", "enum": ["general", "coding", "research", "review"]},
        },
        required=["goal", "when"],
    ),
    FunctionSchema(
        name="list_schedules",
        description="List the Admiral's active schedules with their next run time.",
        properties={}, required=[],
    ),
    FunctionSchema(
        name="cancel_schedule",
        description="Stop a schedule.",
        properties={"schedule_id": {"type": "integer"}},
        required=["schedule_id"],
    ),
    FunctionSchema(
        name="cancel_mind_task",
        description="Stop a running Mind task.",
        properties={"task_id": {"type": "integer", "description": "The task number."}},
        required=["task_id"],
    ),
])


def hud(task: dict) -> dict:
    return {k: task.get(k) for k in ("id", "goal", "role", "provider", "status", "summary", "error", "tool")}


def report_message(task: dict) -> dict:
    if task["status"] == "done":
        body = f"finished. {task.get('summary') or 'No summary.'}"
    else:
        body = f"failed: {task.get('error') or 'unknown error'}"
    return {"role": "user", "content": f"[Mind report] Task {task['id']} ({task['goal'][:120]}) {body}"}


class MindBridge:
    """One per voice session."""

    def __init__(self, tasks: TaskManager, selfmod: SelfMod, gate: Gate, scheduler: Scheduler, llm, worker_ref):
        self.tasks, self.selfmod, self.gate, self.llm, self.worker_ref = tasks, selfmod, gate, llm, worker_ref
        self.scheduler = scheduler
        llm.register_function("schedule_task", self._schedule)
        llm.register_function("list_schedules", self._list_schedules)
        llm.register_function("cancel_schedule", self._cancel_schedule)
        llm.register_function("confirm_action", self._confirm)
        llm.register_function("cancel_action", self._cancel_action)
        llm.register_function("improve_self", self._improve)
        llm.register_function("undo_last_change", self._undo)
        llm.register_function("list_changes", self._changes)
        llm.register_function("delegate_to_mind", self._delegate)
        llm.register_function("mind_status", self._status)
        llm.register_function("cancel_mind_task", self._cancel)

    @property
    def worker(self):
        return self.worker_ref()

    # ---- reflex tools ----
    async def _delegate(self, params: FunctionCallParams):
        goal = params.arguments.get("goal", "").strip()
        role = params.arguments.get("role") or "general"
        if not goal:
            return await params.result_callback({"error": "goal is required"})
        task = await self.tasks.submit(goal, role=role, provenance="admiral")
        await params.result_callback({"task_id": task["id"], "status": "queued", "provider": task["provider"]})

    async def _status(self, params: FunctionCallParams):
        rows = self.tasks.list(limit=6)
        await params.result_callback({"tasks": [
            {"id": t["id"], "goal": t["goal"][:100], "status": t["status"], "summary": t.get("summary")} for t in rows]})

    async def _improve(self, params: FunctionCallParams):
        goal = params.arguments.get("goal", "").strip()
        if not goal:
            return await params.result_callback({"error": "goal is required"})
        change = await self.selfmod.request(goal, provenance="admiral")
        await params.result_callback({"change_id": change["id"], "status": change["status"]})

    async def _undo(self, params: FunctionCallParams):
        change = await self.selfmod.undo_last()
        await params.result_callback({"reverted": change["id"] if change else None,
                                      "status": change["status"] if change else "nothing to undo"})

    async def _changes(self, params: FunctionCallParams):
        await params.result_callback({"changes": [
            {"id": c["id"], "goal": c["goal"][:100], "status": c["status"], "reason": (c.get("reason") or "")[:200]}
            for c in self.selfmod.list(limit=5)]})

    async def on_change(self, change: dict, event: str):
        w = self.worker
        if not w:
            return
        await w.queue_frames([RTVIServerMessageFrame(data={"type": "change", "event": event, "change": {
            k: change.get(k) for k in ("id", "goal", "status", "reason")}})])
        if event in ("promoted", "rejected", "reverted", "revert_failed"):
            verdict = {"promoted": "passed every check and is now live",
                       "rejected": "was rejected by the kernel and did not ship",
                       "reverted": "was rolled back", "revert_failed": "needs the Admiral: automatic rollback failed"}[event]
            msg = f"[Mind report] Self-modification {change['id']} ({change['goal'][:100]}) {verdict}. {change.get('reason') or ''}"
            await w.queue_frames([LLMMessagesAppendFrame(messages=[{"role": "user", "content": msg[:700]}], run_llm=True)])

    async def _schedule(self, params: FunctionCallParams):
        try:
            sc = self.scheduler.create(params.arguments["goal"], params.arguments["when"],
                                       role=params.arguments.get("role") or "general")
            await params.result_callback({"schedule": describe_schedule(sc)})
        except (ValueError, KeyError) as e:  # let the LLM fix the format and try again
            await params.result_callback({"error": f"could not schedule: {e}"})

    async def _list_schedules(self, params: FunctionCallParams):
        await params.result_callback({"schedules": [describe_schedule(sc) for sc in self.scheduler.list()]})

    async def _cancel_schedule(self, params: FunctionCallParams):
        await params.result_callback({"cancelled": self.scheduler.cancel(int(params.arguments.get("schedule_id", 0)))})

    def _pending(self, params: FunctionCallParams) -> dict | None:
        aid = params.arguments.get("action_id")
        return self.gate.get(int(aid)) if aid else self.gate.latest_pending()

    async def _confirm(self, params: FunctionCallParams):
        a = self._pending(params)
        ok = bool(a) and self.gate.resolve(a["id"], True, "confirmed by the Admiral")
        await params.result_callback({"confirmed": a["id"] if ok else None, "note": None if ok else "nothing pending"})

    async def _cancel_action(self, params: FunctionCallParams):
        a = self._pending(params)
        ok = bool(a) and self.gate.resolve(a["id"], False, "cancelled by the Admiral")
        await params.result_callback({"cancelled": a["id"] if ok else None, "note": None if ok else "nothing pending"})

    async def on_action(self, action: dict, event: str):
        w = self.worker
        if not w:
            return
        await w.queue_frames([RTVIServerMessageFrame(data={"type": "action", "event": event, "action": {
            "id": action["id"], "what": describe(action), "status": action["status"], "tainted": bool(action["tainted"])}})])
        if event == "awaiting_confirmation":
            msg = (f"[Action request] Action {action['id']}: the Mind wants to {describe(action)}. This task read external "
                   f"content, so the Admiral must approve. Ask the Admiral to confirm or cancel, in one short sentence.")
            await w.queue_frames([LLMMessagesAppendFrame(messages=[{"role": "user", "content": msg}], run_llm=True)])

    async def _cancel(self, params: FunctionCallParams):
        ok = await self.tasks.cancel(int(params.arguments.get("task_id", 0)))
        await params.result_callback({"cancelled": ok})

    # ---- task events ----
    async def on_task(self, task: dict, event: str):
        w = self.worker
        if not w:
            return
        await w.queue_frames([RTVIServerMessageFrame(data={"type": "task", "event": event, "task": hud(task)})])
        if event in ("done", "failed") and task.get("role") != "selfmod":  # self-mods report via on_change
            await self.report(task)

    async def report(self, task: dict):
        logger.info(f"Reporting Mind task #{task['id']} to the Admiral")
        self.tasks.mark_reported(task["id"])
        await self.worker.queue_frames([LLMMessagesAppendFrame(messages=[report_message(task)], run_llm=True)])

    async def on_connect(self):
        w = self.worker
        for t in self.tasks.list(limit=10, active_only=True):
            await w.queue_frames([RTVIServerMessageFrame(data={"type": "task", "event": t["status"], "task": hud(t)})])
        for t in [t for t in self.tasks.unreported() if t.get("role") != "selfmod"]:
            await self.report(t)
