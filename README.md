# Guppy

A local-first, realtime voice assistant for one Admiral on one Mac, named after Guppy from the Bobiverse.

- **Reflex** (local): mic → VAD/turn detection → STT → small local LLM → Guppy's voice + animated head.
- **Mind** (subscription CLIs: Claude Code / Codex / Grok): real work, tools, and self-modification, running in the background.
- **Kernel**: supervises both, enforces provenance policy, and auto-reverts bad self-edits.

See [ARCHITECTURE.md](ARCHITECTURE.md).

## Layout
- `body/` — Guppy-editable: persona, voice spec, capabilities (MCP servers)
- `kernel/` — voice loop + supervisor (not editable by Guppy)
- `spikes/` — experiments: avatar rig (Tripo → Blender), voice bake-off, live TTS server

Avatar model and art are kept out of this repo (character likeness, personal use only).
