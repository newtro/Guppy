"""Guppy Reflex voice loop.

    .venv/bin/python -m kernel.voice.app        # then open http://127.0.0.1:8765

Browser (mic w/ echo cancellation, avatar) <-WebRTC-> Pipecat pipeline:
  Silero VAD + Smart Turn v3 -> Parakeet STT -> local LLM (mlx_lm.server) -> mood tags -> Guppy TTS
Barge-in: speaking over Guppy interrupts TTS (VAD user-start interrupts the bot).
"""
import asyncio
import os
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
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from kernel.gate import Gate
from kernel.mind.tasks import TaskManager
from kernel.selfmod import SelfMod
from kernel.voice.mind_bridge import TOOLS, MindBridge
from kernel.voice.services import GuppyTTSService, MoodTagProcessor, ParakeetSTTService

ROOT = Path(__file__).resolve().parents[2]
BODY = ROOT / "body"
UI = Path(__file__).parent / "ui"
LLM_MODEL = os.environ.get("GUPPY_REFLEX_MODEL", "mlx-community/Qwen3.5-9B-MLX-4bit")
LLM_PORT = int(os.environ.get("GUPPY_REFLEX_PORT", "8081"))
LLM_URL = f"http://127.0.0.1:{LLM_PORT}/v1"


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
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(
            # 0.4s of silence before Smart Turn judges the turn: a pause after "Guppy," shouldn't end it.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.4))))

    pipeline = Pipeline([
        transport.input(), stt, user_agg, llm, MoodTagProcessor(), tts, transport.output(), assistant_agg,
    ])
    worker = PipelineWorker(pipeline, params=PipelineParams(
        audio_in_sample_rate=16000, audio_out_sample_rate=24000, enable_metrics=True))

    bridge = MindBridge(tasks, selfmod, gate, llm, lambda: worker)
    tasks.listeners.append(bridge.on_task)
    selfmod.listeners.append(bridge.on_change)
    gate.listeners.append(bridge.on_action)

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        greeting = "Aye, Admiral. Guppy online."
        context.add_message({"role": "assistant", "content": f"[deadpan] {greeting}"})
        await worker.queue_frames([TTSSpeakFrame(greeting)])
        await bridge.on_connect()  # HUD state + any Mind reports that finished while offline

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


webrtc = SmallWebRTCRequestHandler()
tasks = TaskManager()
selfmod = SelfMod(tasks)
gate = Gate(tasks, lambda: tasks.config)
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["llm_proc"] = await ensure_reflex_llm()
    # Preload STT + TTS so the first connection is instant.
    await asyncio.gather(ParakeetSTTService().load(), GuppyTTSService(voice_dir=BODY / "persona" / "voice").load())
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
