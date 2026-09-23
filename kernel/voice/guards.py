"""Reliability guards around the small local reflex LLM.

ContextTrimmer   keeps the context to the system prompt + recent turns. The pet stays connected for days; a
                 9B model's tool calling degrades as the context grows.
ToolFiller       speaks a short in-character line the moment a tool call starts, so there's no dead air while the
                 tool runs and the LLM phrases the answer. Lines: body/persona/voice/fillers.json.
PromiseKeeper    if Guppy says he'll have the Mind do something but made no tool call, file the task anyway
                 (the Admiral's last request), so "Aye, I'll check" never silently means nothing.
"""
import json
import random
import re
import time
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

KEEP_MESSAGES = 24
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


class ContextTrimmer(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            msgs = frame.context.get_messages()
            system = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
            rest = [m for m in msgs if not (isinstance(m, dict) and m.get("role") == "system")]
            if len(rest) > KEEP_MESSAGES:
                rest = rest[-KEEP_MESSAGES:]
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
    def __init__(self, fillers_path: Path, **kwargs):
        super().__init__(**kwargs)
        self.path = fillers_path

    def _line(self, names: list[str]) -> str | None:
        try:
            lines = json.loads(self.path.read_text())  # re-read: Guppy may edit it
        except (OSError, json.JSONDecodeError):
            return "One moment, Admiral."
        for n in names:
            if n in SILENT_TOOLS:
                return None
        for n in names:
            if lines.get(n):
                return random.choice(lines[n])
        return random.choice(lines.get("default") or ["One moment, Admiral."])

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, FunctionCallsStartedFrame) and direction == FrameDirection.DOWNSTREAM:
            line = self._line([fc.function_name for fc in frame.function_calls])
            if line:
                await self.push_frame(TTSSpeakFrame(line), FrameDirection.DOWNSTREAM)
