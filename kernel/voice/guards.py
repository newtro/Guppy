"""Reliability guards around the small local reflex LLM.

ContextTrimmer   keeps the context to the system prompt + recent turns. The pet stays connected for days; a
                 9B model's tool calling degrades as the context grows.
ToolFiller       speaks a short in-character line the moment a tool call starts, so there's no dead air while the
                 tool runs and the LLM phrases the answer. Lines: body/persona/voice/fillers.json.
PromiseKeeper    if Guppy says he'll have the Mind do something but made no tool call, file the task anyway
                 (the Admiral's last request), so "Aye, I'll check" never silently means nothing.
"""
import asyncio
import json
import random
import re
import time
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InterruptionFrame,
    Frame,
    FunctionCallsStartedFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

KEEP_MESSAGES = 24     # trim when the conversation grows past this...
KEEP_AFTER_TRIM = 12   # ...down to this, so the cached prefix breaks rarely
PROMISE = re.compile(
    r"\b(the mind|i'?ll (have|get|check|look|find|pull|fetch|ask|dispatch|send)|let me (check|look|find)|on it|dispatch)",
    re.I)
NOT_FROM_ADMIRAL = ("[Mind report]", "[Action request]", "(The Admiral")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


NOW_TAG = "[Current local time]"


def now_note() -> str:
    from datetime import datetime
    now = datetime.now().astimezone()
    return f"{NOW_TAG} {now.strftime('%A, %B %-d, %Y, %-I:%M %p %Z')}"


class ContextTrimmer(FrameProcessor):
    """Also stamps the Admiral's latest message with the current local time (a small model will otherwise invent
    a time rather than call a tool).

    Everything here keeps the prompt prefix byte-stable, because the reflex model (Qwen3.5, hybrid attention) can
    only reuse its prompt cache for an exact prefix: the time goes on the newest user message (once, never
    rewritten), not in the system prompt, and trimming drops a large block at a time rather than one message per turn.
    A change to the system prompt costs a full re-read of it (~5s)."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            msgs = frame.context.get_messages()
            system = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
            rest = [m for m in msgs if not (isinstance(m, dict) and m.get("role") == "system")]
            if system:  # the model accepts one system message only; strip a legacy time note
                system = [{**system[0], "content": str(system[0].get("content", "")).split("\n\n" + NOW_TAG)[0]}]
            if rest and isinstance(rest[-1], dict) and rest[-1].get("role") == "user":
                content = rest[-1].get("content")
                if isinstance(content, str) and NOW_TAG not in content:
                    rest[-1] = {**rest[-1], "content": f"{content}\n\n{now_note()}"}
            if len(rest) > KEEP_MESSAGES:
                rest = rest[-KEEP_AFTER_TRIM:]
                while rest and not (isinstance(rest[0], dict) and rest[0].get("role") == "user"):
                    rest.pop(0)  # never start mid tool-call exchange
            frame.context.set_messages(system + rest)
        await self.push_frame(frame, direction)


class PromiseKeeper(FrameProcessor):
    def __init__(self, context, tasks, **kwargs):
        super().__init__(**kwargs)
        self.context, self.tasks = context, tasks
        self._text, self._last_tool_call = "", 0.0

    def _last_user(self) -> str:
        for m in reversed(self.context.get_messages()):
            if isinstance(m, dict) and m.get("role") == "user":
                return _text(m.get("content")).strip()
        return ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, FunctionCallsStartedFrame):
            self._last_tool_call = time.time()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._text = ""
        elif isinstance(frame, LLMTextFrame):
            self._text += frame.text
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._check()
        await self.push_frame(frame, direction)

    async def _check(self):
        said, request = self._text, self._last_user()
        if not PROMISE.search(said) or time.time() - self._last_tool_call < 30:
            return
        if not request or request.startswith(NOT_FROM_ADMIRAL):
            return
        logger.warning(f"Reflex promised work without a tool call; filing it: {request[:80]!r}")
        await self.tasks.submit(request, role="general", provenance="admiral")
        self._last_tool_call = time.time()


SILENT_TOOLS = {"confirm_action", "cancel_action", "cancel_mind_task", "cancel_schedule", "undo_last_change"}


class ToolFiller(FrameProcessor):
    """Also covers a slow start: if the LLM has produced nothing STALL_S after it starts (a cold prompt cache after
    a restart or a persona change), speak a generic filler right away and skip the tool filler for that reply."""

    STALL_S = 1.2

    def __init__(self, fillers_path: Path, **kwargs):
        super().__init__(**kwargs)
        self.path = fillers_path
        self._stall_task = None
        self._stalled = False

    def _lines(self) -> dict:
        try:
            return json.loads(self.path.read_text())  # re-read: Guppy may edit it
        except (OSError, json.JSONDecodeError):
            return {}

    def _line(self, names: list[str]) -> str | None:
        lines = self._lines()
        for n in names:
            if n in SILENT_TOOLS:
                return None
        for n in names:
            if lines.get(n):
                return random.choice(lines[n])
        return random.choice(lines.get("default") or ["One moment, Admiral."])

    def _cancel_stall(self):
        if self._stall_task:
            self._stall_task.cancel()
            self._stall_task = None

    async def _stall(self):
        await asyncio.sleep(self.STALL_S)
        self._stall_task, self._stalled = None, True
        line = random.choice(self._lines().get("stall") or ["One moment, Admiral."])
        logger.debug(f"LLM slow to start; stall filler: {line!r}")
        await self.push_frame(TTSSpeakFrame(line), FrameDirection.DOWNSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._cancel_stall()
                self._stalled = False
                self._stall_task = asyncio.create_task(self._stall())
            elif isinstance(frame, (LLMTextFrame, FunctionCallsStartedFrame, LLMFullResponseEndFrame)):
                self._cancel_stall()
            elif isinstance(frame, (InterruptionFrame, CancelFrame, EndFrame)):
                self._cancel_stall()
        await self.push_frame(frame, direction)
        if isinstance(frame, FunctionCallsStartedFrame) and direction == FrameDirection.DOWNSTREAM:
            line = self._line([fc.function_name for fc in frame.function_calls])
            if line and not self._stalled:
                await self.push_frame(TTSSpeakFrame(line), FrameDirection.DOWNSTREAM)
            self._stalled = False
