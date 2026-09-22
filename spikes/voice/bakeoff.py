"""Guppy voice bake-off: design a voice from text on each model, measure speed on this Mac.

.venv/bin/python bakeoff.py <qwen|breeze|voxcpm>
Writes out/<model>/<n>.wav and appends a JSON line to out/results.jsonl.
"""
import json, sys, time
from pathlib import Path
import numpy as np
import soundfile as sf
import mlx.core as mx
from mlx_audio.tts.utils import load_model

GUPPY = (
    "A middle-aged male alien naval officer. Deep, gravelly, slightly wet and throaty voice "
    "with a clipped military cadence. Dry, deadpan, understated delivery; precise enunciation; unhurried."
)
LINES = [
    "Aye, Admiral. The sprint board is updated, and the blog post is scheduled for nine tomorrow.",
    "I have rewritten my own email module. Again. You're welcome.",
    "Incoming message from the build server. It is, predictably, on fire.",
]
EVENT_LINE = "(sigh) Admiral, that is the fourth time you have asked me that today."

MODELS = {
    "qwen":   dict(repo="mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit", stream=True),
    "breeze": dict(repo="mlx-community/Breeze-TTS-2-mlx-8bit", stream=True),
    "voxcpm": dict(repo="mlx-community/VoxCPM2-8bit", stream=False),
}

def gen(model, name, text, stream):
    if name == "qwen":
        it = model.generate_voice_design(text=text, instruct=GUPPY, language="English", stream=stream, streaming_interval=0.32)
    elif name == "breeze":
        it = model.generate(text=text, instruct=GUPPY, cfg_scale=4, stream=stream, streaming_interval=0.32)
    else:
        it = model.generate(text=text, instruct=GUPPY)
    t0 = time.perf_counter(); first = None; chunks = []; sr = None
    for r in it:
        a = np.array(r.audio, dtype=np.float32)
        if first is None: first = time.perf_counter() - t0
        chunks.append(a); sr = r.sample_rate
    total = time.perf_counter() - t0
    audio = np.concatenate(chunks)
    return audio, sr, first, total

def main(name):
    cfg = MODELS[name]
    out = Path(__file__).parent / "out" / name; out.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter(); model = load_model(cfg["repo"]); load_s = time.perf_counter() - t
    gen(model, name, "Aye.", cfg["stream"])  # warm-up (compile kernels)
    lines = LINES + ([EVENT_LINE] if name == "breeze" else [])
    rows = []
    for i, text in enumerate(lines):
        audio, sr, first, total = gen(model, name, text, cfg["stream"])
        dur = len(audio) / sr
        sf.write(out / f"{i}.wav", audio, sr)
        rows.append(dict(line=i, ttfa_s=round(first, 3), gen_s=round(total, 2), audio_s=round(dur, 2), rtf=round(total / dur, 3)))
        print(name, rows[-1], flush=True)
    res = dict(model=name, repo=cfg["repo"], load_s=round(load_s, 1), peak_mem_gb=round(mx.get_peak_memory() / 1e9, 2), runs=rows)
    with open(out.parent / "results.jsonl", "a") as f: f.write(json.dumps(res) + "\n")
    print("RESULT", json.dumps(res))

if __name__ == "__main__":
    main(sys.argv[1])
