"""Stage 1: Breeze designs Guppy voice variants on a reference passage.
Stage 2: clone each variant into fast Qwen3-TTS Base models; time it.

.venv/bin/python design_clone.py design     # -> out/design/v<n>.wav
.venv/bin/python design_clone.py clone      # -> out/clone/<model>_v<n>.wav, out/clone_results.jsonl
"""
import json, sys, time
from pathlib import Path
import numpy as np
import soundfile as sf
import mlx.core as mx
from mlx_audio.tts.utils import load_model

OUT = Path(__file__).parent / "out"
BASE = ("a middle-aged male alien naval officer. Deep, gravelly, slightly wet and throaty voice with a clipped "
        "military cadence. Dry, deadpan, understated delivery; precise enunciation; unhurried.")
VARIANTS = [
    (1, "A " + BASE),
    (2, "A " + BASE),  # same prompt, different seed
    (3, "An older, " + BASE + " Very low pitch, heavy gravel, a little weary."),
    (4, "A " + BASE + " A faint bubbling, amphibian burble at the edges of words."),
    (5, "A " + BASE + " Sardonic and bone-dry, like a veteran officer who has seen everything twice."),
]
REF_TEXT = ("Aye, Admiral. All systems are nominal, the sprint board is current, "
            "and I have taken the liberty of fixing the build. Again. Shall I proceed?")
TEST_TEXT = "Admiral, the blog post is live, the site is deployed, and nothing is on fire. For now."
CLONERS = {
    "qwen17": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
    "qwen06": "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
}


def collect(it):
    t0 = time.perf_counter(); first = None; chunks = []; sr = None
    for r in it:
        if first is None: first = time.perf_counter() - t0
        chunks.append(np.array(r.audio, dtype=np.float32)); sr = r.sample_rate
    return np.concatenate(chunks), sr, first, time.perf_counter() - t0


def design():
    d = OUT / "design"; d.mkdir(parents=True, exist_ok=True)
    model = load_model("mlx-community/Breeze-TTS-2-mlx-8bit")
    for n, instruct in VARIANTS:
        mx.random.seed(1000 + n)
        audio, sr, _, total = collect(model.generate(text=REF_TEXT, instruct=instruct, cfg_scale=4))
        sf.write(d / f"v{n}.wav", audio, sr)
        print(f"design v{n}: {len(audio)/sr:.1f}s audio in {total:.1f}s", flush=True)
    (d / "ref_text.txt").write_text(REF_TEXT)


def clone():
    d = OUT / "clone"; d.mkdir(parents=True, exist_ok=True)
    refs = sorted((OUT / "design").glob("v*.wav"))
    for key, repo in CLONERS.items():
        model = load_model(repo)
        collect(model.generate(text="Aye.", ref_audio=str(refs[0]), ref_text=REF_TEXT, stream=True, streaming_interval=0.32))  # warm-up
        for ref in refs:
            audio, sr, first, total = collect(model.generate(
                text=TEST_TEXT, ref_audio=str(ref), ref_text=REF_TEXT, stream=True, streaming_interval=0.32))
            dur = len(audio) / sr
            sf.write(d / f"{key}_{ref.stem}.wav", audio, sr)
            row = dict(model=key, ref=ref.stem, ttfa_s=round(first, 3), rtf=round(total / dur, 3), audio_s=round(dur, 2))
            print("clone", row, flush=True)
            with open(OUT / "clone_results.jsonl", "a") as f: f.write(json.dumps(row) + "\n")
        print(f"clone {key} peak_mem_gb={mx.get_peak_memory()/1e9:.2f}", flush=True)
        del model; mx.clear_cache(); mx.reset_peak_memory()


if __name__ == "__main__":
    {"design": design, "clone": clone}[sys.argv[1]]()
