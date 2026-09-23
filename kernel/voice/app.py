"""Guppy Reflex voice loop.

    .venv/bin/python -m kernel.voice.app        # then open http://127.0.0.1:8765

Browser (mic w/ echo cancellation, avatar) <-WebRTC-> Pipecat pipeline:
  Silero VAD + Smart Turn v3 -> Parakeet STT -> local LLM (mlx_lm.server) -> mood tags -> Guppy TTS
Barge-in: speaking over Guppy interrupts TTS (VAD user-start interrupts the bot).
"""
import asyncio
import json
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import BotStoppedSpeakingFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_start.transcription_user_turn_start_strategy import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_start.wake_phrase_user_turn_start_strategy import WakePhraseUserTurnStartStrategy
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from kernel.gate import Gate
from kernel.scheduler import Scheduler
from kernel.mind.tasks import TaskManager
from kernel.selfmod import SelfMod
from kernel.voice.mind_bridge import TOOLS, MindBridge
from kernel.voice.guards import ContextTrimmer, PromiseKeeper, ToolFiller
from kernel.voice.services import GuppyTTSService, MoodTagProcessor, ParakeetSTTService

ROOT = Path(__file__).resolve().parents[2]
BODY = ROOT / "body"
UI = Path(__file__).parent / "ui"
LLM_MODEL = os.environ.get("GUPPY_REFLEX_MODEL", "mlx-community/Qwen3.5-9B-MLX-4bit")
LLM_PORT = int(os.environ.get("GUPPY_REFLEX_PORT", "8081"))
LLM_URL = f"http://127.0.0.1:{LLM_PORT}/v1"


class WakeGate(WakePhraseUserTurnStartStrategy):
    """Pipecat's wake-phrase gate, plus a manual wake (clicking Guppy's head) and, in every-turn mode, back to
    sleep as soon as Guppy has finished answering, so a TV talking right after can't slip into the awake window.
    A bare "Guppy" (nothing else said) keeps him awake for the actual request."""

    def __init__(self, *args, every_turn: bool = False, bare_aliases: list[str] = (), **kwargs):
        super().__init__(*args, single_activation=every_turn, **kwargs)
        self._every_turn, self._bare_wake = every_turn, False
        self._bare_aliases = set(bare_aliases)  # how the STT mishears a lone "Guppy"; only count when said alone

    def force_awake(self, reason: str = "click"):
        self._bare_wake = True  # a click is like saying just "Guppy"
        self._transition_to_awake(reason)

    async def process_frame(self, frame):
        if isinstance(frame, TranscriptionFrame) and not self.awake:
            words = re.sub(r"[^\w\s]", "", frame.text.lower()).split()
            if 1 <= len(words) <= 2 and set(words) - {"hey", "ok", "okay"} <= self._bare_aliases and words[-1] in self._bare_aliases:
                logger.info(f"Bare wake from likely mishearing {frame.text!r}")
                self.force_awake("bare")
                await self._call_event_handler("on_wake_phrase_detected", "guppy")
                return ProcessFrameResult.STOP
        if isinstance(frame, TranscriptionFrame) and self._every_turn:
            words = re.sub(r"[^\w\s]", "", frame.text.lower()).split()
            rest = [w for w in words if not any(re.fullmatch(p, w) for p in self._phrases) and w not in ("hey", "ok", "okay")]
            self._bare_wake = not rest
        if (self._every_turn and isinstance(frame, BotStoppedSpeakingFrame) and self.awake and not self._bare_wake):
            self._transition_to_idle()
        return await super().process_frame(frame)

    @property
    def awake(self) -> bool:
        return self.state.value == "awake"


def voice_spec() -> dict:
    return json.loads((BODY / "persona" / "voice" / "voice.json").read_text())


def persona() -> str:
    return (BODY / "persona" / "guppy.md").read_text()  # re-read per session: Guppy may edit it


async def ensure_reflex_llm() -> subprocess.Popen | None:
    """Start mlx_lm.server unless one is already listening, then warm the weights."""
    proc = None
    async with httpx.AsyncClient() as http:
        try:
            await http.get(f"{LLM_URL}/models", timeout=1)
        except httpx.HTTPError:
            (ROOT / ".run").mkdir(exist_ok=True)
            log = open(ROOT / ".run" / "reflex_llm.log", "a")
            proc = subprocess.Popen(
                [sys.executable, "-m", "mlx_lm.server", "--model", LLM_MODEL, "--host", "127.0.0.1",
                 "--port", str(LLM_PORT), "--chat-template-args", '{"enable_thinking": false}', "--max-tokens", "300"],
                stdout=log, stderr=subprocess.STDOUT)
            for _ in range(300):
                try:
                    await http.get(f"{LLM_URL}/models", timeout=1); break
                except httpx.HTTPError:
                    await asyncio.sleep(1)
        # mlx_lm.server loads weights lazily on the first completion
        await http.post(f"{LLM_URL}/chat/completions", timeout=600, json={
            "model": LLM_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
    logger.info(f"Reflex LLM ready: {LLM_MODEL}")
    return proc


async def run_bot(connection: SmallWebRTCConnection):
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    stt = ParakeetSTTService()
    llm = OpenAILLMService(base_url=LLM_URL, api_key="local", model=LLM_MODEL)
    tts = GuppyTTSService(voice_dir=BODY / "persona" / "voice")
    await asyncio.gather(stt.load(), tts.load())

    context = LLMContext(messages=[{"role": "system", "content": persona()}], tools=TOOLS)
    wake_cfg = voice_spec().get("wake", {})
    wake = None
    start = [VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()]
    if wake_cfg.get("enabled"):
        # Asleep: speech without the wake word is dropped and can't start a turn or interrupt Guppy.
        wake = WakeGate(phrases=wake_cfg.get("phrases", ["guppy"]), timeout=wake_cfg.get("awake_timeout_s", 30),
                        every_turn=bool(wake_cfg.get("every_turn")), bare_aliases=wake_cfg.get("bare_aliases", []))
        start = [wake, *start]
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(
            # 0.4s of silence before Smart Turn judges the turn: a pause after "Guppy," shouldn't end it.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.4)),
            user_turn_strategies=UserTurnStrategies(start=start)))

    pipeline = Pipeline([
        transport.input(), stt, user_agg, ContextTrimmer(), llm, ToolFiller(BODY / "persona" / "voice" / "fillers.json"),
        PromiseKeeper(context, tasks), MoodTagProcessor(),
        tts, transport.output(), assistant_agg,
    ])
    worker = PipelineWorker(pipeline, params=PipelineParams(
        audio_in_sample_rate=16000, audio_out_sample_rate=24000, enable_metrics=True))

    bridge = MindBridge(tasks, selfmod, gate, scheduler, llm, lambda: worker)
    tasks.listeners.append(bridge.on_task)
    selfmod.listeners.append(bridge.on_change)
    gate.listeners.append(bridge.on_action)

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        greeting = "Aye, Admiral. Guppy online."
        context.add_message({"role": "assistant", "content": f"[deadpan] {greeting}"})
        await worker.queue_frames([TTSSpeakFrame(greeting)])
        await bridge.on_connect()  # HUD state + any Mind reports that finished while offline
        await send_wake_state()

    async def send_wake_state():
        await worker.queue_frames([RTVIServerMessageFrame(data={"type": "wake", "awake": wake.awake if wake else True,
                                                                "enabled": bool(wake)})])

    if wake:
        @wake.event_handler("on_wake_phrase_detected")
        async def on_wake(strategy, phrase):
            logger.info(f"Wake word heard: {phrase!r}")
            await send_wake_state()

        @wake.event_handler("on_wake_phrase_timeout")
        async def on_sleep(strategy):
            logger.info("Guppy went back to sleep (no activity)")
            await send_wake_state()

    @worker.rtvi.event_handler("on_client_message")
    async def on_client_message(rtvi, msg):
        if msg.type == "wake" and wake:
            wake.force_awake("click")
            await send_wake_state()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Admiral disconnected")
        tasks.listeners.remove(bridge.on_task)
        selfmod.listeners.remove(bridge.on_change)
        gate.listeners.remove(bridge.on_action)
        await runner.cancel()

    await runner.run()


async def watch_reflex_llm():
    """Restart the reflex LLM server if it dies (launchd only watches the kernel process itself)."""
    while True:
        await asyncio.sleep(30)
        try:
            async with httpx.AsyncClient() as http:
                await http.get(f"{LLM_URL}/models", timeout=5)
        except httpx.HTTPError:
            logger.warning("Reflex LLM is down; restarting it")
            try:
                state["llm_proc"] = await ensure_reflex_llm()
            except Exception:
                logger.exception("Reflex LLM restart failed")


webrtc = SmallWebRTCRequestHandler()
tasks = TaskManager()
selfmod = SelfMod(tasks)
gate = Gate(tasks, lambda: tasks.config)
scheduler = Scheduler(tasks)
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["llm_proc"] = await ensure_reflex_llm()
    # Preload STT + TTS so the first connection is instant.
    await asyncio.gather(ParakeetSTTService().load(), GuppyTTSService(voice_dir=BODY / "persona" / "voice").load())
    scheduler.start()
    state["llm_watch"] = asyncio.create_task(watch_reflex_llm())
    logger.info("Guppy is listening: http://127.0.0.1:8765")
    yield
    await webrtc.close()
    if state.get("llm_proc"):
        state["llm_proc"].terminate()


app = FastAPI(lifespan=lifespan)


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    async def on_connection(connection: SmallWebRTCConnection):
        background_tasks.add_task(run_bot, connection)
    return await webrtc.handle_web_request(request=request, webrtc_connection_callback=on_connection)


@app.patch("/api/offer")
async def ice(request: SmallWebRTCPatchRequest):
    await webrtc.handle_patch_request(request)
    return {"status": "success"}


@app.get("/")
async def index():
    return FileResponse(UI / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/tasks")
async def list_tasks(limit: int = 20):
    return tasks.list(limit=limit)


@app.post("/api/tasks")
async def create_task(body: dict):
    return await tasks.submit(body["goal"], role=body.get("role", "general"), provenance="admiral",
                              provider=body.get("provider"))


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int):
    return {**(tasks.get(task_id) or {}), "events": tasks.events(task_id)}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    return {"cancelled": await tasks.cancel(task_id)}


@app.post("/api/gate/call")
async def gate_call(body: dict, request: Request):
    """Called by kernel/gateway.py for every capability tool call. May block (hold / confirmation)."""
    if request.client.host != "127.0.0.1":
        return {"decision": "deny", "reason": "gate is local-only"}
    return await gate.decide(int(body["task_id"]), body["capability"], body["tool"], body.get("effect", "act"),
                             body.get("arguments", {}), gateway_tainted=bool(body.get("tainted")))


@app.post("/api/gate/taint")
async def gate_taint(body: dict):
    gate.taint(int(body["task_id"]), body.get("capability", ""), body.get("tool", ""))
    return {"ok": True}


@app.get("/api/gate/actions")
async def gate_actions(limit: int = 20):
    return gate.list(limit=limit)
# Deliberately no HTTP endpoint to confirm or cancel actions: only the Admiral's voice can.


@app.get("/api/schedules")
async def list_schedules(all: bool = False):
    return scheduler.list(include_disabled=all)


@app.post("/api/schedules")
async def create_schedule(body: dict):
    try:
        return scheduler.create(body["goal"], body["when"], role=body.get("role", "general"))
    except ValueError as e:
        return {"error": str(e)}


@app.delete("/api/schedules/{sid}")
async def delete_schedule(sid: int):
    return {"cancelled": scheduler.cancel(sid)}


@app.get("/api/changes")
async def list_changes(limit: int = 10):
    return selfmod.list(limit=limit)


@app.post("/api/changes")
async def create_change(body: dict):
    return await selfmod.request(body["goal"], provenance=body.get("provenance", "admiral"))


@app.post("/api/changes/undo")
async def undo_change():
    return await selfmod.undo_last() or {"status": "nothing to undo"}


@app.get("/test.wav")
async def test_wav():  # spoken test prompt for ?test mode (generated locally, see .run/)
    return FileResponse(ROOT / ".run" / "test.wav")


app.mount("/avatar", StaticFiles(directory=BODY / "avatar"), name="avatar")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("GUPPY_PORT", "8765")))
