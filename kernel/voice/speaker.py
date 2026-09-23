"""Speaker verification: Guppy only listens to the Admiral's voice.

Every utterance the STT receives is embedded with a WeSpeaker ResNet34 model (sherpa-onnx, local CPU) and compared
by cosine similarity with the Admiral's enrolled voiceprint. Speech that doesn't match is dropped before the wake
word or the LLM ever sees it, so a TV, a podcast, or another person can't talk to Guppy.

Enrollment ("Guppy, learn my voice") records a few utterances through the same mic and audio path Guppy normally
hears, averages them into a voiceprint, and calibrates the threshold from how consistent the Admiral's own samples
are. The voiceprint is biometric data: it lives only on this Mac (VOICEPRINT), never in the repo.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from loguru import logger

MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
MODEL_PATH = Path.home() / ".cache" / "guppy" / "wespeaker_en_voxceleb_resnet34_LM.onnx"
VOICEPRINT = Path.home() / "Library" / "Application Support" / "Guppy" / "voiceprint.json"
MIN_ENROLL_SECONDS = 1.2


class SpeakerVerifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._extractor = None
        self.print: np.ndarray | None = None
        self.threshold = cfg.get("max_threshold", 0.85)
        self._load_print()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    @property
    def enrolled(self) -> bool:
        return self.print is not None

    def _load_print(self):
        if VOICEPRINT.exists():
            d = json.loads(VOICEPRINT.read_text())
            self.print = np.array(d["embedding"], dtype=np.float32)
            self.threshold = float(d["threshold"])

    def _model(self):
        if self._extractor is None:
            import sherpa_onnx
            if not MODEL_PATH.exists():
                import urllib.request
                MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                logger.info("Downloading speaker model")
                urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL_PATH), num_threads=2))
        return self._extractor

    def embed(self, pcm16: bytes, sample_rate: int) -> np.ndarray:
        ext = self._model()
        s = ext.create_stream()
        s.accept_waveform(sample_rate, np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        s.input_finished()
        e = np.array(ext.compute(s), dtype=np.float32)
        return e / (np.linalg.norm(e) + 1e-9)

    def score(self, emb: np.ndarray) -> float:
        return float(emb @ self.print) if self.print is not None else 1.0

    def is_admiral(self, pcm16: bytes, sample_rate: int) -> tuple[bool, float]:
        if not (self.enabled and self.enrolled):
            return True, 1.0
        s = self.score(self.embed(pcm16, sample_rate))
        return s >= self.threshold, s

    def save_enrollment(self, embeddings: list[np.ndarray]) -> float:
        """Average the samples into a voiceprint; threshold = the Admiral's own leave-one-out consistency minus a margin."""
        E = np.stack(embeddings)
        mean = E.mean(axis=0)
        mean /= np.linalg.norm(mean)
        loo = []
        for i in range(len(E)):
            rest = np.delete(E, i, axis=0).mean(axis=0)
            loo.append(float(E[i] @ (rest / np.linalg.norm(rest))))
        margin = self.cfg.get("margin", 0.15)
        threshold = float(np.clip(min(loo) - margin, self.cfg.get("min_threshold", 0.45), self.cfg.get("max_threshold", 0.85)))
        VOICEPRINT.parent.mkdir(parents=True, exist_ok=True)
        VOICEPRINT.write_text(json.dumps({"embedding": mean.tolist(), "threshold": threshold, "samples": len(E),
                                          "self_similarity": loo, "enrolled_at": time.time()}))
        VOICEPRINT.chmod(0o600)
        self.print, self.threshold = mean, threshold
        logger.info(f"Voiceprint saved: {len(E)} samples, self-similarity {min(loo):.2f}-{max(loo):.2f}, threshold {threshold:.2f}")
        return threshold

    def forget(self):
        VOICEPRINT.unlink(missing_ok=True)
        self.print = None
