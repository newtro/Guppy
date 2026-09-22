# Guppy — Architecture (draft v0.1, 2026-09-22)

> "Aye, Admiral."

A local-first, realtime voice assistant for one person on one Mac (M4 Max, 36GB). Speech, avatar and fast replies run locally. The frontier "mind" is a subscription CLI (Claude Code, Codex, or Grok Build). Guppy can rewrite and extend most of himself with near-zero human approvals.

---

## 1. Principles

1. **Local where it's cheap and fast; cloud where it's smart.** Ears, voice, face and reflexes run on-device. Reasoning and coding go through the subscription CLIs.
2. **Small immutable kernel, large mutable body.** Guppy can change anything except the thing that can undo his changes.
3. **Every change is a commit; every commit can be reverted automatically.**
4. **Trust follows provenance, not approvals.** The Admiral's requests run autonomously. Anything triggered by external content is sandboxed.
5. **Warm processes only.** Nothing on the voice path spawns a process per turn.

---

## 2. Topology

```
 ┌─────────────── REFLEX (local, realtime, always responsive) ───────────────┐
 │ mic → AEC → wake word → VAD → turn detect → STT                            │
 │        → Reflex LLM (local MLX) → TTS → speakers + avatar visemes/emotion  │
 │ Owns the conversation. Never blocks on the Mind.                           │
 └───────────────┬───────────────────────────────────────▲────────────────────┘
                 │ task (async: goal + context + provenance)│ progress / result events
                 ▼                                          │ (reflex decides when & how to speak them)
 ┌─────────────── MIND (cloud CLIs, separate processes, slow & smart) ────────┐
 │ Task queue → worker pool of warm adapters: Claude · Codex · Grok           │
 │ Many tasks can run in parallel; each has its own session + worktree        │
 │ Uses capabilities via MCP ─► email · devops · blog · site · memory · self  │
 └────────────────────────────────────────────────────────────────────────────┘
 ┌─────────────── KERNEL (immutable to Guppy) — hosts & guards both ──────────┐
 │ Supervisor: process lifecycle for Reflex, Mind workers, capabilities       │
 │ Provenance/policy gate on every MCP call · Audit log · Secrets (Keychain)  │
 │ Scheduler · Self-mod deploy / watchdog / rollback · Eval runner            │
 └────────────────────────────────────────────────────────────────────────────┘
                                  │ starts / restarts / reverts
                 ┌────────────────▼──────────── BODY (git repo, Guppy-editable) ──────────────┐
                 │ persona/ (prompt, voice spec, emotion map)   routing/ (reflex vs mind rules)│
                 │ capabilities/  (each = MCP server):                                          │
                 │   email(Remail) · devops(Azure DevOps) · blog · site(johnnycode.ai/Lightsail)│
                 │   scheduler-jobs · memory · calendar · web · self (introspection)           │
                 │ avatar/ (model, moods)   evals/ (behavior tests)   tests/                    │
                 └────────────────────────────────────────────────────────────────────────────┘
```

Language: **Python 3.12+** for kernel and body. Pipecat, MLX and mlx-audio are all Python. Capabilities may use any language, since they only need to speak MCP.

---

## 3. Voice pipeline (local)

| Slot | Primary | Fallback |
|---|---|---|
| Framework | Pipecat (local transport) | LiveKit Agents |
| Echo cancel / barge-in | macOS voice-processing I/O unit | — |
| Wake word | livekit-wakeword, custom "Guppy" | openWakeWord |
| VAD | Silero VAD | — |
| End of turn | Pipecat Smart Turn v3 (CoreML, ~12ms) | LiveKit turn detector |
| STT | Parakeet TDT v3 on the Neural Engine (FluidAudio) or parakeet-mlx | Voxtral Realtime 4-bit (MLX) |
| TTS | **Bake-off:** Qwen3-TTS (VoiceDesign → Base clone), Breeze TTS 2 (MLX 8-bit), VoxCPM2 | Chatterbox Turbo (tags such as `[sigh]`), Kokoro (speed) |

Conversation modes:
- **Wake word**, then a follow-up window of about 8s with no wake word needed.
- **"Guppy, stay"** switches to open conversation. **"Dismissed"** switches back.

**Latency budget** (end of your speech → first audio): turn detection 100ms + STT 150ms + reflex LLM first sentence 250ms + TTS first audio 200ms, for **≈700ms**. The mind never sits on this path: the reflex replies straight away and the mind reports back when it's done.

---

## 4. Persona and voice

- **Character:** terse, dry, literal-minded, loyal; addresses the user as "Admiral"; "Aye" / "Aye, Admiral"; a deadpan fish. The persona is a body file, so Guppy can tune it himself.
- **Voice is designed, not cloned.** Use a voice-design model with a text description (deep, gravelly, clipped, dry, faintly wet/alien timbre). Save the best reference clip plus its transcript, then clone *that* clip into the streaming TTS. Add a light formant/pitch effect in post if needed. We do not clone real actors (audiobook narrator, film voice actors).
- **Emotion tags:** the LLM emits inline tags from one vocabulary, for example `[deadpan] [smug] [exasperated] [alarmed] [pleased] [thinking]`. The Reflex strips them and drives both the TTS style/instruction and the avatar mood from the same tag.

---

## 5. Avatar

- **v1:** a Three.js scene in a borderless always-on-top web view, served by the kernel on localhost. Uses **TalkingHead** (MIT): viseme blendshapes, moods, blink/idle/gesture. Lip-sync comes from TTS phoneme timestamps if the chosen TTS provides them; otherwise **HeadAudio** classifies visemes from the PCM stream in the browser.
- **Model:** a custom fish-alien head in the style of Admiral Ackbar (GLB), with about 15 Oculus visemes and 6–8 emotion shapes. For personal desktop use only; never appears in published content.
- **Idle states:** listening (eyes track), thinking (while the mind works), speaking, and away/sleeping.
- **v2 (experimental):** a neural pass (LivePortrait/MuseTalk via CoreML) over the rendered rig. It may not work on a non-human face, so it's out of scope until v1 is solid.
- **HUD** panel next to the head: current tasks, mind activity, recent audit entries, and undo buttons.

---

## 6. Brains

### 6.1 Reflex (local)
A Qwen3-class 8–14B instruct model, 4-bit, served by MLX (to be benchmarked). Jobs:
- Immediate spoken reply for chit-chat and acknowledgements.
- Classify each utterance: `chat | quick-tool | task | self-mod`.
- Summarise mind output into speech-length lines.

It gets read-only quick tools (time, calendar lookup, task status).

### 6.2 Mind (subscription CLIs, provider-abstracted)
A small pool of warm long-lived processes per provider (so parallel tasks don't queue), behind a common adapter:

```python
class BrainAdapter(Protocol):
    id: Literal["claude", "codex", "grok"]
    async def start(self, *, cwd, model=None, effort=None, mcp_servers=(), system_prompt=None,
                    autonomy="bypass", resume_id=None, fork=False) -> str: ...  # session id
    def send(self, text: str) -> "TurnHandle": ...
    async def interrupt(self) -> None: ...              # barge-in
    async def steer(self, text: str) -> None: ...       # codex native; others interrupt+send
    async def close(self) -> None: ...
    events: AsyncIterator["BrainEvent"]  # ready | text_delta | thinking_delta | tool_start |
                                         # tool_end | approval_request | turn_end | rate_limit | error
```

| Provider | Transport | Warm TTFT (measured) |
|---|---|---|
| Claude Code 2.1.270 | `claude -p --input-format stream-json --output-format stream-json --include-partial-messages` | ~0.8s |
| Codex 0.153.4 | `codex app-server` (JSON-RPC stdio: `thread/start`, `turn/start`, `turn/interrupt`, `turn/steer`) | ~3.2s at high effort, lower with a faster model or effort (to be measured) |
| Grok Build 1.0.40 | `grok agent stdio` (ACP: `session/new`, `session/prompt`) | ~0.9s |

- **The Mind never speaks directly.** It receives tasks from the Reflex and returns events and results. The Reflex turns them into speech at a natural moment ("Admiral, the sprint digest is done"), and can interrupt, steer or cancel a running task by voice.
- **Routing** is a config file in the body, per Mind role. For example: `research/answers: grok`, `coding/self-mod: claude`, `second-opinion review: codex`. Conversation is not a Mind role; it always belongs to the Reflex. Failover to the next provider happens on `rate_limit` or `error`.
- **Isolation:** Mind workers are separate processes run by the supervisor. A hung or crashed CLI can't stall the voice loop.
- **Trim the preamble.** Cold calls carry 11–25k tokens of default tools and skills, so restrict each CLI to our MCP servers (`--strict-mcp-config`, `--tools`, `--ignore-user-config`).
- **Terms of service.** Drive the **unmodified CLI binaries** with your own login for personal use only. Do not use the Claude Agent SDK on subscription auth, and never extract OAuth tokens for direct API calls. If usage outgrows "ordinary individual use", switch that adapter to an API key; the interface doesn't change.

---

## 7. Provenance and policy (zero approvals, safely)

Each task and mind session carries a **provenance** label:
- `admiral`: originated from the Admiral's voice or the HUD.
- `external`: any external content has entered its context (email bodies, web pages, work-item text, comments). Sessions are tainted as a whole; once tainted, a session stays tainted.

Every MCP tool declares an **effect class**: `read`, `draft`, `act` (send, publish, deploy, update a work item), or `self-mod`.

| | read | draft | act | self-mod |
|---|---|---|---|---|
| admiral | ✅ | ✅ | ✅ (with undo window) | ✅ (pipeline §8) |
| external | ✅ | ✅ | ⏸ spoken check | ⛔ → becomes a proposal for the Admiral |

- A **spoken check** is one line: "Admiral, an email from X asks me to Y. Proceed?" Recurring jobs the Admiral created run as `admiral`, but their outputs are still gated per the table if external content was read. The Admiral can pre-authorise specific job and action pairs, such as "the Monday digest may email me".
- **Undo window:** outbound email and publish actions are delayed 60s by default, during which "Guppy, cancel that" stops them. Everything is written to the audit log.
- **Rate limits and blast radius:** caps on emails per hour, deploys per day, and so on, enforced in the kernel.
- **Secrets** live in the macOS Keychain. The kernel injects them into each capability's environment. They are never in the body repo and never visible to the mind.

---

## 8. Self-modification pipeline

```
request (voice / Guppy's own idea / failing eval)
  → plan (mind)                             
  → git worktree off body@main  ─►  mind (coding role) edits code + tests, bypass perms, cwd=worktree
  → kernel gate: lint · unit tests · capability contract tests · evals/ · MCP handshake smoke test
  → promote: fast-forward main, hot-reload affected capability processes (others untouched)
  → watchdog (10 min): crash/error-rate/latency vs baseline
  → pass: speak a one-line summary ("New skill: DevOps standup digest. Tested.")
    fail: auto `git revert`, restart, record the failure in memory, tell the Admiral
```

- **Scope:** capabilities, persona, routing, avatar moods, evals, prompts, and body core logic are all editable autonomously. The **kernel is not**. It lives in a separate directory that Guppy can only read (file ownership plus an allowlist enforced when the kernel launches the mind), so the rollback path can't be broken.
- **Kernel upgrades:** Guppy may write a *proposal* (a branch plus rationale) to change the kernel. The Admiral merges it.
- **"Guppy, undo that" / "roll back to yesterday"** map to git operations on the body.
- **Proactive improvement:** a nightly job reviews audit logs and failed requests, then proposes or ships fixes through the same pipeline.
- **Evals** are a growing set of scripted conversations with expected tool calls or outcomes. Guppy adds one for every bug he fixes.

---

## 9. Day-one capabilities (MCP servers in the body)

| Capability | Backing system | Notes |
|---|---|---|
| email | Remail (self-hosted) | read/search/draft/send; inbound = `external` |
| devops | Azure DevOps (SCV2) | query, create, update work items, sprint summaries |
| blog | johnnycode.ai blog | draft → publish, with the undo window |
| site | johnnycode.ai on the Lightsail VM | edit in repo, deploy through existing workflow, health check, rollback |
| scheduler | kernel scheduler (SQLite) | "every Monday at 8, …"; jobs run with the creator's provenance |
| memory | SQLite + local embeddings | facts about the Admiral, preferences, episodic log |
| calendar / web / notes | TBD | |
| self | kernel introspection | status, logs, capability list, trigger self-mod |

---

## 10. Build order

1. **Voice loop skeleton:** Pipecat, STT, reflex LLM and Kokoro, with barge-in. Target under 800ms.
2. **Mind adapters:** Claude first, then Grok and Codex. Reflex→Mind task queue, background tasks with spoken completion.
3. **Kernel supervisor, body repo and self-mod pipeline**, with auto-revert. Guppy builds his own first capability.
4. **Provenance and policy gate**, audit log, undo window, and Keychain secrets broker.
5. **Capabilities:** email, devops, scheduler, then blog and site. Guppy builds the later ones himself where possible.
6. **TTS bake-off and Guppy voice design;** emotion tags wired end to end.
7. **Avatar v1:** TalkingHead with a placeholder model, then the custom fish-alien model, plus the HUD.

## 11. Open questions / benchmarks

- TTS time-to-first-audio and real-time factor on the M4 Max for Qwen3-TTS, Breeze 2 (8-bit) and VoxCPM2.
- Which local reflex model best balances speed and quality.
- Codex latency with a faster model or lower effort.
- Avatar style: 3D (sculpted GLB) or 2D (Rive). Who makes the model?
- Mic setup: the built-in array vs an external mic when the room is noisy.
