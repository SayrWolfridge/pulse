# Pulse — ClawHub Listing Draft

*Last updated: March 9, 2026 — v0.5.5, 1,232 tests*

## Tagline
**"Give your AI agent a heartbeat — autonomous cognition for OpenClaw"**

## Short Description (280 chars max)
Pulse is a persistent daemon that gives AI agents self-directed initiative. Instead of waiting for crons or human commands, your agent thinks for itself — noticing changes, prioritizing urgency, and acting autonomously.

## Category
- **Primary:** Agent Infrastructure
- **Secondary:** Autonomy, Monitoring

## Tags
`autonomy` `self-directed` `drives` `motivation` `monitoring` `daemon` `ai-agent` `consciousness` `MIT` `open-source`

## Features

### 🧠 Autonomous Cognition
- **Drive engine** with 6 built-in motivation categories (goals, curiosity, emotions, learning, social, system)
- **Pressure accumulation** — unfulfilled drives get louder over time
- **Self-wake triggers** — agent decides when to think, not you
- **CORTEX_EXT** — learning gap detector that surfaces silent recurring errors automatically (escalates patterns 3+ times)

### 📡 Passive Monitoring
- **Filesystem sensor** — watches workspace for changes (goals.json, notes, agent output)
- **Conversation sensor** — detects when human is active (suppresses interruptions)
- **System sensor** — monitors daemon health (memory, disk)
- **Discord sensor** — detects channel silence, DM activity, mentions
- **Git sensor** — watches for uncommitted changes, stale branches, merge conflicts
- **Web/RSS sensor** — monitors feeds and URLs for content changes

### 🎯 Smart Triggering
- **Rules mode** (default) — simple threshold math, zero AI calls
- **Model mode** (optional) — context-aware decisions via local LLM (Ollama)
- **Rate limiting** — max turns/hour + cooldown to prevent runaway triggers
- **Conversation suppression** — never interrupts active human chat

### 🔧 Self-Modifying
- **Mutation system** — agent evolves its own config at runtime
- **Guardrails** — prevents self-disabling, extreme changes, mutation spam
- **Audit log** — every self-modification is timestamped and explained

### 📊 Observability
- **Prometheus `/metrics` endpoint** — scrape drive pressures, trigger counts, feedback outcomes, uptime
- **Visual dashboard** (`http://localhost:7842`) — real-time drive bars, trigger history, live WebSocket stream
- **RL-lite feedback learner** — EMA-based adaptive weight system; reinforces high-performing drives, suppresses low-performing ones over time (persisted across restarts)
- **WebSocket `/stream`** — push updates every 5 seconds including drive state, learner multipliers, phenotype
- **`/state/learner` endpoint** — per-drive reinforcement multipliers [0.7–1.3], success rates, event history

### 🤝 Multi-Agent Coordination
- **Peer sync** (`src/peer_sync.py`) — polls sibling Pulse instances via their `/status` endpoint
- **Social deference** — backs off a drive when a peer is already working on the same category
- **Emotional contagion** — low-salience THALAMUS signals from peer mood state
- **Peer availability awareness** — escalates if a peer goes offline after 3 consecutive failures

### 🧬 Identity Portability (GENOME v2)
- **`pulse genome export`** — captures full identity bundle: modules, phenotype snapshot, drive pressures/weights, RL-lite learned EMA multipliers, sensor health config
- **`pulse genome import`** — restores learned weights with two merge policies (overwrite or blend)
- **Backward-compatible** — v1 genomes still import via legacy path
- **Use case:** migrate your agent's personality to a new machine with one command

### 🩺 Health Diagnostics
- **`pulse doctor`** — one command to inspect your full runtime health
  - Python version, config validity, state/log dirs, daemon status, API reachability, OpenClaw gateway probe
  - `--json` for machine-readable output; non-zero exit on failure for CI integration
  - Respects `PULSE_CONFIG` env var for non-default config locations

### 🚀 Production-Ready
- **Portable** — runs on Mac, Linux, Pi, VPS, Docker
- **Lightweight** — <50 MB RAM, <0.1% CPU idle
- **Persistent** — state survives restarts, migrations, hardware changes
- **Zero OpenClaw coupling** — communicates purely via webhook API
- **NIST AI RMF aligned** — documented in `docs/COMPLIANCE.md` (GOVERN/MAP/MEASURE/MANAGE mapping)

## Use Cases

1. **Personal AI assistant** — proactive memory maintenance, goal tracking, creative prompts
2. **Trading bot** — rapid response to market opportunities, risk alerts
3. **Research agent** — monitors papers, datasets, experiments; triggers analysis
4. **Content creator** — detects ideas, drafts, publishing opportunities
5. **DevOps agent** — watches logs, metrics, deployments; escalates issues

## Installation

```bash
# Via pip
pip install pulse-agent

# Or clone
git clone https://github.com/astra-ventures/pulse.git
cd pulse
pip install -e .
```

## Quick Start

```bash
# 1. Configure
mkdir -p ~/.pulse/config
cp config/pulse.example.yaml ~/.pulse/config/pulse.yaml
nano ~/.pulse/config/pulse.yaml  # set webhook_url + webhook_token

# 2. Run
python3 -m pulse

# 3. Test
curl http://localhost:9719/health

# 4. Check health
pulse doctor

# 5. View dashboard
open http://localhost:7842

# 6. Export identity
pulse genome export > my-agent.genome.json
```

See [docs/deployment.md](docs/deployment.md) for production setup (systemd, Docker, LaunchAgent).

## Configuration Example

```yaml
drives:
  trigger_threshold: 5.0
  categories:
    goals:
      weight: 1.0
      sources: ["goals.json"]
    curiosity:
      weight: 0.8
      sources: ["curiosity.json"]

sensors:
  filesystem:
    watch_paths: [".", "memory/*.md"]
  conversation:
    activity_threshold_seconds: 300
  discord:
    enabled: true
    silence_threshold_minutes: 60
  git:
    enabled: true
    watch_paths: ["."]

openclaw:
  min_trigger_interval: 1800  # 30 min cooldown
  max_turns_per_hour: 10
```

## Screenshots / Demo

Use `SCREENSHOT_GUIDE.md` to capture:
- Screenshot 1: Dashboard at `localhost:7842` — drive bars, learner multipliers, trigger history
- Screenshot 2: `/state` before feedback (drive pressure visible)
- Screenshot 3: `/state` after feedback (drive decay visible)
- Screenshot 4: `pulse doctor` health output
- Short GIF: file change → trigger → agent action → feedback loop

## Documentation

- [Architecture](docs/architecture.md) — how Pulse works
- [Configuration](docs/configuration.md) — complete reference (all sensors, drives, RL-lite)
- [Deployment](docs/deployment.md) — production setup (systemd, Docker, LaunchAgent)
- [Compliance](docs/COMPLIANCE.md) — NIST AI RMF alignment
- [Examples](examples/) — sample configs (personal assistant, trading bot)

## Roadmap

### Phase 1: Core (Done ✅)
- Drive engine + sensors + evaluator
- State persistence + migrations
- Self-modification system
- Health monitoring (SPINE, AMYGDALA, IMMUNE)

### Phase 2: Polish (Done ✅)
- `pulse doctor` health diagnostics CLI
- CORTEX_EXT learning gap detector
- Unified `python3 -m pulse <cmd>` entry point
- Full Black formatting + CI lint stability

### Phase 3: Integrations (Done ✅)
- Discord sensor (channel silence detection)
- X/Twitter sensor (mentions, trends)
- Calendar sensor (upcoming events)
- Git sensor (uncommitted changes, stale branches)
- Web/RSS sensor (feed monitoring)
- 1,033 tests (v0.4.x era)

### Phase 4: Advanced Cognition (Done ✅)
- **Prometheus metrics** — `GET /metrics`, 18 gauges + counters, zero deps (v0.5.1)
- **RL-lite feedback learner** — EMA bandit, per-drive reinforcement, 51 tests (v0.5.2)
- **Dashboard learner card** — real-time multiplier visualization, `/state/learner` endpoint (v0.5.3)
- **Multi-agent coordination** — peer sync, social deference, emotional contagion (v0.5.4)
- 1,152 tests (v0.5.4)

### Phase 5: Identity & Launch (Current 🔄)
- **GENOME v2** — identity bundle export/import with learned weights + blend merge policy ✅ (v0.5.5)
- **COMPLIANCE.md** — NIST AI RMF alignment documentation ✅ (v0.5.5)
- PyPI publish (`pip install pulse-agent`) — Josh action
- ClawHub submission — Josh action
- Product Hunt launch — Josh action
- **1,232 tests** (v0.5.5)

## Pricing

- **Core:** Free, open source (MIT)
- **Pro (planned):** $29/mo — advanced sensors, priority support, cloud sync
- **Enterprise (planned):** $99/mo — multi-agent, custom integrations, consulting
