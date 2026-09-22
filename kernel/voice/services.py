"""Local Pipecat services for Guppy's Reflex: Parakeet STT, cloned-voice TTS, mood tags.

All MLX work (STT + TTS) runs on one dedicated thread: MLX is happiest single-threaded, and the
two never need to overlap (the user and Guppy don't talk at once; barge-in cancels TTS first).
The reflex LLM runs out-of-process (mlx_lm.server), so it never contends with this thread.
"""
import asyncio
import json
import re
import threading
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, LLMTextFrame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

MLX = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
_MODELS: dict[str, object] = {}  # loaded once per process, shared by every session


async def on_mlx(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(MLX, fn, *args)


class ParakeetSTTService(SegmentedSTTService):
    """Transcribes each VAD-bounded utterance with NVIDIA Parakeet TDT (MLX)."""

    def __init__(self, *, model: str = "mlx-community/parakeet-tdt-0.6b-v3", **kwargs):
        super().__init__(settings=STTSettings(model=model, language=Language.EN), **kwargs)
        self._model_id = model
        self._model = None

    @property
    def wants_wav_segments(self) -> bool:
        return False  # raw int16 PCM

    async def load(self):
        if self._model_id not in _MODELS:
            from parakeet_mlx import from_pretrained
            _MODELS[self._model_id] = await on_mlx(from_pretrained, self._model_id)
            self._model = _MODELS[self._model_id]
            noise = (np.random.default_rng(0).standard_normal(16000 * 3) * 300).astype(np.int16).tobytes()
            for _ in range(2):  # warm-up: compile kernels for typical utterance lengths
                await on_mlx(self._transcribe, noise, 16000)
            logger.info(f"Parakeet loaded: {self._model_id}")
        self._model = _MODELS[self._model_id]

    def _transcribe(self, pcm: bytes, sr: int) -> str:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        target = self._model.preprocessor_config.sample_rate
        if sr != target:  # cheap linear resample; segments are short
            audio = np.interp(np.linspace(0, len(audio), int(len(audio) * target / sr), endpoint=False),
                              np.arange(len(audio)), audio).astype(np.float32)
        mel = get_logmel(mx.array(audio), self._model.preprocessor_config)
        return self._model.generate(mel)[0].text.strip()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            if self._model is None:
                await self.load()
            import time
            t0 = time.perf_counter()
            text = await on_mlx(self._transcribe, audio, self.sample_rate)
            logger.debug(f"STT {len(audio) / 2 / self.sample_rate:.1f}s audio in {time.perf_counter() - t0:.3f}s")
            if text:
                logger.info(f"Admiral: {text}")
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), Language.EN)
        except Exception as e:
            yield ErrorFrame(error=f"Parakeet STT failed: {e}")


class GuppyTTSService(TTSService):
    """Guppy's designed voice, cloned live with Qwen3-TTS (MLX) from body/persona/voice."""

    MODEL_RATE = 24000

    def __init__(self, *, voice_dir: Path, **kwargs):
        super().__init__(push_start_frame=True, push_stop_frames=True,
                         settings=TTSSettings(model=None, voice="guppy", language=Language.EN), **kwargs)
        self._spec = json.loads((voice_dir / "voice.json").read_text())
        self._ref_audio = str(voice_dir / self._spec["reference_audio"])
        self._ref_text = self._spec["reference_text"]
        self._model = None
        self._resampler = create_stream_resampler()

    def can_generate_metrics(self) -> bool:
        return True

    async def load(self):
        repo = self._spec["live_tts"]["model"]
        if repo not in _MODELS:
            from mlx_audio.tts.utils import load_model
            _MODELS[repo] = await on_mlx(load_model, repo)
            self._model = _MODELS[repo]
            for line in ("Aye, Admiral. All systems are nominal.", "Aye."):  # warm-up: compile kernels
                await (await self._collect(line, threading.Event(), None))
            logger.info(f"Guppy voice loaded: {repo}")
        self._model = _MODELS[repo]

    def _stream(self, text: str):
        return self._model.generate(text=text, ref_audio=self._ref_audio, ref_text=self._ref_text,
                                    stream=True, streaming_interval=self._spec["live_tts"]["streaming_interval"])

    async def _collect(self, text: str, stop: threading.Event, queue: asyncio.Queue | None):
        loop = asyncio.get_running_loop()

        def work():
            import time
            t0, n = time.perf_counter(), 0
            for r in self._stream(text):
                if stop.is_set():
                    break
                n += 1
                if n == 1:
                    logger.debug(f"TTS first chunk in {time.perf_counter() - t0:.3f}s (in MLX thread) :: {text[:40]!r}")
                if queue is not None:
                    pcm = (np.clip(np.array(r.audio, dtype=np.float32), -1, 1) * 32767).astype(np.int16).tobytes()
                    loop.call_soon_threadsafe(queue.put_nowait, pcm)
            if queue is not None:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        return loop.run_in_executor(MLX, work)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        if self._model is None:
            await self.load()
        stop, queue = threading.Event(), asyncio.Queue()
        logger.debug(f"TTS request :: {text[:40]!r}")
        job = await self._collect(text, stop, queue)
        try:
            await self.start_tts_usage_metrics(text)
            while (pcm := await queue.get()) is not None:
                await self.stop_ttfb_metrics()
                audio = await self._resampler.resample(pcm, self.MODEL_RATE, self.sample_rate)
                yield TTSAudioRawFrame(audio=audio, sample_rate=self.sample_rate, num_channels=1, context_id=context_id)
            await job
        except Exception as e:
            yield ErrorFrame(error=f"Guppy TTS failed: {e}")
        finally:
            stop.set()  # barge-in / cancellation: stop generating at the next chunk
            await self.stop_ttfb_metrics()


class MoodTagProcessor(FrameProcessor):
    """Strips leading [mood] tags from LLM text and sends the mood to the client's face."""

    TAG = re.compile(r"\[(deadpan|smug|exasperated|alarmed|pleased)\]\s*", re.I)
    ANY_TAG = re.compile(r"\[[a-z]+\]\s*", re.I)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._buf = ""
        self._head = True  # still at the start of a reply (tag not yet resolved)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMTextFrame):
            if type(frame).__name__ in ("LLMFullResponseStartFrame", "LLMFullResponseEndFrame"):
                if self._buf:  # flush anything held back at the end of a reply
                    await self.push_frame(LLMTextFrame(self._buf), direction)
                self._buf, self._head = "", True
            await self.push_frame(frame, direction)
            return
        if not self._head:
            await self.push_frame(frame, direction)
            return
        self._buf += frame.text
        stripped = self._buf.lstrip()
        if stripped.startswith("[") and "]" not in stripped and len(stripped) < 16:
            return  # tag still arriving
        m = self.TAG.match(stripped)
        if m:
            await self.push_frame(RTVIServerMessageFrame(data={"type": "mood", "mood": m.group(1).lower()}))
            stripped = stripped[m.end():]
        else:
            stripped = self.ANY_TAG.sub("", stripped, count=1) if stripped.startswith("[") else stripped
        self._buf, self._head = "", False
        if stripped:
            await self.push_frame(LLMTextFrame(stripped), direction)
