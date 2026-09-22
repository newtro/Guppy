"""Guppy live TTS server (spike): streams Guppy's cloned voice and serves the avatar page.

.venv/bin/python guppy_tts_server.py [--port 8765]
  GET  /            -> spikes/avatar/web (static)
  POST /speak       {"text": "..."} -> chunked audio/L16 (int16 mono PCM); header X-Sample-Rate
"""
import argparse, json, threading, time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import numpy as np
from mlx_audio.tts.utils import load_model

ROOT = Path(__file__).resolve().parents[2]
VOICE_DIR = ROOT / "body" / "persona" / "voice"
WEB = ROOT / "spikes" / "avatar" / "web"
spec = json.loads((VOICE_DIR / "voice.json").read_text())
REF_AUDIO = str(VOICE_DIR / spec["reference_audio"])
REF_TEXT = spec["reference_text"]

print("loading", spec["live_tts"]["model"], flush=True)
model = load_model(spec["live_tts"]["model"])
lock = threading.Lock()  # one generation at a time on the GPU


def stream(text):
    return model.generate(text=text, ref_audio=REF_AUDIO, ref_text=REF_TEXT, stream=True,
                          streaming_interval=spec["live_tts"]["streaming_interval"])


with lock:  # warm-up so the first request doesn't pay kernel compile time
    for _ in stream("Aye."): pass
print("ready", flush=True)


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # chunked streaming needs 1.1

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if self.path != "/speak":
            return self.send_error(404)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        text = (body.get("text") or "").strip()[:1000]
        if not text:
            return self.send_error(400, "text required")
        self.send_response(200)
        self.send_header("Content-Type", "audio/L16")
        self.send_header("X-Sample-Rate", "24000")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        t0 = time.perf_counter(); first = None; samples = 0
        with lock:
            try:
                for r in stream(text):
                    pcm = (np.clip(np.array(r.audio, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes()
                    if first is None: first = time.perf_counter() - t0
                    samples += len(pcm) // 2
                    self.wfile.write(f"{len(pcm):X}\r\n".encode() + pcm + b"\r\n"); self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                return  # client barged in / closed
            except Exception:
                import traceback; traceback.print_exc(); self.close_connection = True; return
        total = time.perf_counter() - t0
        print(f"speak: {samples/24000:.1f}s audio, first chunk {first or 0:.3f}s, total {total:.2f}s :: {text[:60]!r}", flush=True)

    def log_message(self, fmt, *args):
        if "POST" in str(args[0] if args else ""): return
        super().log_message(fmt, *args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), partial(Handler, directory=str(WEB))).serve_forever()
