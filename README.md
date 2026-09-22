# Guppy

A local-first, realtime voice assistant for one Admiral on one Mac, named after Guppy from the Bobiverse.

- **Reflex** (local): mic → VAD/turn detection → STT → small local LLM → Guppy's voice + animated head.
- **Mind** (subscription CLIs: Claude Code / Codex / Grok): real work, tools, and self-modification, running in the background.
- **Kernel**: supervises both, enforces provenance policy, and auto-reverts bad self-edits.

See [ARCHITECTURE.md](ARCHITECTURE.md).

## Run the voice loop
```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -r pyproject.toml
.venv/bin/python -m kernel.voice.app
```
Open http://127.0.0.1:8765 in Chrome and click **Talk to Guppy**. First start downloads models (~10 GB).
The avatar model (`body/avatar/guppy_head.glb`) is not in the repo; build it with `spikes/avatar/`.

## Layout
- `body/` — Guppy-editable: persona, voice spec, capabilities (MCP servers)
- `kernel/` — voice loop + supervisor (not editable by Guppy)
- `spikes/` — experiments: avatar rig (Tripo → Blender), voice bake-off, live TTS server

Avatar model and art are kept out of this repo (character likeness, personal use only).
