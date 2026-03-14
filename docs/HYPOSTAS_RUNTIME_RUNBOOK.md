# HypostasRuntime (Pulse v2) — Runbook

This is the single source of truth for how the Pulse v2 runtime is wired and operated on this machine.

## 0) What this is

**HypostasRuntime** is Pulse v2’s always-on cognition layer:
- persistent identity + goals + episodic memory + emotion + relationships
- ThoughtLoop (reflect/plan/compress)
- ResponseEngine (context → local model → reply)
- Proactive loop (candidate → compose → dispatch)
- ChannelBridge (inbound/outbound routing)

Runtime HTTP base: `http://127.0.0.1:9723`

## 1) Source code locations

Runtime modules:
- `pulse/src/runtime/` (HypostasRuntime + engines)

Key files:
- Orchestrator + HTTP endpoints: `pulse/src/runtime/__init__.py`
- Entry point: `pulse/src/runtime/__main__.py`
- ThoughtLoop: `pulse/src/runtime/thought_loop.py`
- ResponseEngine: `pulse/src/runtime/response_engine.py`
- ProactiveEngine: `pulse/src/runtime/proactive_engine.py`
- ProactiveDispatcher: `pulse/src/runtime/proactive_dispatcher.py`
- ChannelBridge: `pulse/src/runtime/channel_bridge.py`

## 2) Model policy (locked)

**Local model lock-in:** Iris v4 via Ollama
- ThoughtLoop model: `iris-70b-v4:latest`
- ResponseEngine ollama model: `iris-70b-v4:latest`

Guardrails:
- ThoughtLoop plan gating: skips planning if plan inputs unchanged (<1h)
- Plan JSON retry: one retry at `temperature=0.0` if parse fails

## 3) Runtime HTTP endpoints

Health & status:
- `GET /runtime/health`
- `GET /runtime/status`

Context:
- `GET /runtime/context?format=compact|standard|full&person=josh`
- `POST /runtime/context/prime`

Respond:
- `POST /runtime/respond` body: `{ "message": "...", "person": "josh", "format": "compact", "max_tokens": 400 }`

Proactive:
- `GET /runtime/proactive`
- `GET /runtime/proactive/top`
- `POST /runtime/proactive/sent`
- `POST /runtime/proactive/deliver`

Bridge (Day 17):
- `POST /runtime/bridge/receive`
- `POST /runtime/bridge/send`
- `GET /runtime/bridge/status`
- `POST /runtime/bridge/proactive`

## 4) Process management (LaunchAgent)

LaunchAgent files in repo:
- `pulse/deploy/ai.hypostas.runtime.plist`
- `pulse/deploy/run-runtime.sh`

Installed paths:
- Plist: `~/Library/LaunchAgents/ai.hypostas.runtime.plist`
- Script: `~/.pulse/run-runtime.sh`
- Env: `~/.pulse/.env`

Logs:
- `~/.pulse/logs/runtime-stdout.log`
- `~/.pulse/logs/runtime-stderr.log`

**Important:** avoid `nohup` duplicates. There should be one runtime process bound to :9723.

## 5) Outbound delivery (queue → Signal)

ChannelBridge includes an **OpenClawHandler** that queues outbound messages to:
- StateEngine key: `proactive.openclaw_outbound`

Each queued entry has `status=pending|sent|failed` and timestamps.

Delivery worker:
- OpenClaw cron job: `hypostas-outbound-drainer`
- Schedule: every 5 minutes
- Model: `anthropic/claude-sonnet-4-6`

Behavior:
- Reads `~/.pulse/state/hypostas-state.json`
- Sends `entry.text` to Josh via Signal
- Marks entry `sent`

## 6) Inbound routing (gap)

Current state:
- Runtime accepts inbound via `POST /runtime/bridge/receive` (or `/runtime/respond`).

Remaining integration work:
- Automatic routing from OpenClaw channel inbound events → runtime receive.

## 7) Cron model fix

Some cron jobs were configured to use `openai-codex/gpt-5.2-spark`, which fails under the current Codex auth.

Temporary fix:
- Those jobs were updated to `anthropic/claude-sonnet-4-6`.

## 8) Related specs / notes

- Day 17 spec: `pulse/docs/PULSE_V2_DAY17_SPEC.md`
- Daily build log (narrative): `workspace/memory/2026-03-14.md`

