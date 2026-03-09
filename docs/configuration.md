# Pulse Configuration

All configuration lives in `config/pulse.yaml` (or `~/.pulse/pulse.yaml` / `~/.pulse/config/pulse.yaml`).

---

## Quick Start

```bash
# Copy the example config
mkdir -p ~/.pulse/config
cp config/pulse.example.yaml ~/.pulse/config/pulse.yaml

# Edit the required fields
nano ~/.pulse/config/pulse.yaml
```

**Required:**
- `openclaw.webhook_url` — your OpenClaw webhook endpoint
- `openclaw.webhook_token` — auth token (set in OpenClaw gateway config)

**Optional but recommended:**
- `daemon.integration` — set to `"iris"` if using Iris-style workspace, or `"default"`
- `workspace.root` — path to your OpenClaw workspace

---

## Full Reference

### OpenClaw Integration

```yaml
openclaw:
  webhook_url: "http://localhost:8080/hooks/agent"
  webhook_token: "${PULSE_HOOK_TOKEN}"  # or hardcode (not recommended)
  min_trigger_interval: 1800  # seconds (30 min cooldown between triggers)
  max_turns_per_hour: 10      # rate limit to prevent runaway triggers
```

**Environment variable expansion:**
- `${VAR_NAME}` resolves to `os.environ["VAR_NAME"]`
- Useful for keeping secrets out of git
- Example: `export PULSE_HOOK_TOKEN=your-secret-token`

### Daemon

```yaml
daemon:
  loop_interval_seconds: 30     # how often to check drives
  health_port: 9719              # HTTP status endpoint
  integration: "default"         # "default", "iris", or custom module path
  log_level: "INFO"              # DEBUG, INFO, WARNING, ERROR
```

**Integration types:**
- `default` — generic OpenClaw setup
- `iris` — Iris-specific workspace layout (goals.json, curiosity.json, etc.)
- Custom: `"mypackage.integrations.custom"` (must subclass `Integration`)

### Prometheus Metrics

Pulse exposes a Prometheus-compatible metrics endpoint at `GET /metrics` on the same health port. No extra configuration needed — it's always available.

```bash
# Quick test
curl http://localhost:9720/metrics
```

**Metrics exposed:**

| Metric | Type | Description |
|--------|------|-------------|
| `pulse_info{version, python_version}` | gauge | Build metadata (always 1) |
| `pulse_uptime_seconds` | gauge | Seconds since daemon started |
| `pulse_turn_count_total` | counter | Total agent turns fired |
| `pulse_drives_pressure{drive}` | gauge | Current pressure per drive (0.0–5.0+) |
| `pulse_drives_weight{drive}` | gauge | Configured weight per drive |
| `pulse_triggers_total{reason}` | counter | Successful triggers by reason |
| `pulse_trigger_failures_total{reason}` | counter | Failed trigger attempts by reason |
| `pulse_feedback_total{outcome}` | counter | Feedback calls by outcome (success/partial/blocked) |
| `pulse_instincts_fired_total{instinct}` | counter | Instinct executions by name |

**Prometheus scrape config:**

```yaml
scrape_configs:
  - job_name: pulse
    static_configs:
      - targets: ['localhost:9720']
    metrics_path: /metrics
    scrape_interval: 30s
```

Zero external dependencies — implements the [Prometheus text exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/) directly.

### Drives

```yaml
drives:
  trigger_threshold: 5.0        # total weighted pressure to trigger
  pressure_rate: 0.01           # pressure added per minute per drive
  max_pressure: 20.0            # ceiling for any single drive
  decay_on_success: 0.7         # multiply pressure by 0.3 on successful turn
  decay_on_failure: 0.3         # multiply pressure by 0.7 on failed turn
  
  categories:
    goals:
      weight: 1.0
      sources:
        - "goals.json"
    
    curiosity:
      weight: 0.8
      sources:
        - "curiosity.json"
    
    emotions:
      weight: 1.2
      sources:
        - "emotional-landscape.json"
    
    learning:
      weight: 0.9
      sources:
        - "hypotheses.json"
    
    social:
      weight: 0.7
      sources:
        - "x_notifications.json"  # example: X mentions
    
    system:
      weight: 0.6
      sources:
        - "/var/log/system.log"   # example: system health
```

**How drives work:**
- Each category has a **weight** (importance multiplier)
- **Sources** are files that Pulse watches for changes
- When a source file changes, the drive spikes
- Over time, drives accumulate pressure at `pressure_rate * weight`
- When **total weighted pressure** > `trigger_threshold`, a turn triggers

**Tuning tips:**
- Higher `trigger_threshold` → fewer, more urgent triggers
- Higher `pressure_rate` → faster pressure accumulation (more sensitive)
- Adjust weights to prioritize certain drives (e.g. `goals: 1.5` if revenue is critical)

### Sensors

Pulse ships **eight sensors** organized into two tiers:

| Tier | Sensors | Purpose |
|------|---------|---------|
| **Core** (always available) | `filesystem`, `conversation`, `system` | Workspace activity, human presence, daemon health |
| **Phase 3** (optional integrations) | `discord`, `twitter`, `git`, `web`, `calendar` | External signals — silence, mentions, code state, RSS, schedule |

---

#### Core Sensors

```yaml
sensors:
  filesystem:
    enabled: true
    watch_paths:
      - "."                      # workspace root
      - "memory/*.md"
      - "projects/*/README.md"
    ignore_patterns:
      - "*.pyc"
      - "__pycache__"
      - ".git/*"
      - "node_modules/*"
    debounce_seconds: 2.0        # wait 2s after last change before reporting
  
  conversation:
    enabled: true
    session_file: "memory/main-session.txt"  # OpenClaw main session log
    activity_threshold_seconds: 300          # consider "active" if < 5 min since last message
    size_threshold_kb: 100                   # only check if file > 100 KB (performance)
  
  system:
    enabled: true
    check_interval_seconds: 60   # how often to check memory/disk
    memory_threshold_mb: 100     # warn if daemon uses > 100 MB
    disk_threshold_gb: 5         # warn if < 5 GB free
```

**Filesystem sensor:**
- Uses watchdog library (efficient, event-driven)
- `watch_paths` can be relative (to workspace) or absolute
- Globs supported: `"memory/*.md"`, `"projects/*/src/**/*.py"`
- `debounce_seconds` prevents spam from rapid file edits

**Conversation sensor:**
- Monitors OpenClaw session file size + mtime
- **Suppresses triggers** when human is actively chatting (no interruptions during conversations)
- Only processes large files (avoids reading 5 MB logs every 30s)

**System sensor:**
- Monitors Pulse daemon's own health (memory usage, free disk)
- Warns if daemon is leaking memory or disk is nearly full
- Drives addressed: `system`

---

#### Phase 3 Sensors

All Phase 3 sensors are **disabled by default** — enable only the ones you've configured credentials for.

##### Discord Sensor

Detects silence in monitored channels and spikes the `social` drive.

```yaml
sensors:
  discord:
    enabled: false
    channels:
      - "1234567890"               # Discord channel IDs to monitor
    silence_threshold_minutes: 60  # spike social drive after 60 min of silence
    request_timeout: 10            # API call timeout (seconds)
```

- **Drive wiring:** channel silence → `social.spike(0.15)`
- **Requires:** Discord bot token in `DISCORD_BOT_TOKEN` env var (or OpenClaw's bot credentials)
- **Use case:** Agent notices when a channel has gone quiet and proactively checks in

##### X / Twitter Sensor

Polls Twitter API v2 for `@mention` silence — spikes `social` drive if no mentions for a configurable window.

```yaml
sensors:
  twitter:
    enabled: false
    username: "iamIrisAI"          # your X handle (without @)
    silence_threshold_minutes: 360 # 6 hours — X moves slower than Discord
    bearer_token: "${TWITTER_BEARER_TOKEN}"  # or bearer_token_env: "TWITTER_BEARER_TOKEN"
    max_results: 10                # mentions to fetch per poll
    request_timeout: 15
```

- **Drive wiring:** mention silence → `social.spike(0.1)`
- **Requires:** Twitter API v2 Bearer Token (free tier sufficient)
- **State:** Last-seen mention ID persisted to disk — survives restarts without re-counting
- **Note:** Silence threshold defaults to 360 min because X engagement is slower-paced than Discord

##### Git Sensor

Monitors one or more git repositories for uncommitted work, untracked files, and stale pushes.

```yaml
sensors:
  git:
    enabled: false
    repos:
      - "/Users/iris/.openclaw/workspace"   # absolute paths to repos
      - "/Users/iris/.openclaw/workspace/pulse"
    stale_push_minutes: 120    # spike if last push was > 2 hours ago AND changes exist
    fetch_remote: false        # set true to run `git fetch` and detect commits_behind
    request_timeout: 10        # subprocess timeout per repo (seconds)
```

- **Drive wiring:**
  - `uncommitted_changes` or `untracked_files > 0` → `goals.spike(0.15)`
  - `stale_push` → `goals.spike(0.2)` (stronger — uncommitted + time elapsed)
  - `commits_behind > 0` → `growth.spike(0.1)` (upstream has updates worth pulling)
- **Graceful degradation:** Non-repo paths, missing `git` binary, and network errors are logged and skipped — never crash the daemon
- **Note:** `fetch_remote: true` adds latency per loop; keep false unless `commits_behind` detection is needed

##### Web Sensor (RSS / Atom)

Monitors RSS and Atom feeds for new content. Spikes the `curiosity` drive when fresh items appear.

```yaml
sensors:
  web:
    enabled: false
    feeds:
      - "https://hnrss.org/frontpage"               # Hacker News
      - "https://feeds.feedburner.com/TheAtlantic"  # any RSS/Atom URL
    check_interval_minutes: 30    # minimum gap between feed polls (per-feed)
    max_items_per_feed: 20        # cap items counted per check (avoids floods)
    request_timeout: 15           # HTTP fetch timeout (seconds)
```

- **Drive wiring:** new feed items → `curiosity.spike(0.15)`
- **Parsing:** Pure stdlib (`xml.etree.ElementTree`) — no feedparser dependency
- **Supports:** RSS 2.0 and Atom 1.0 formats
- **First-run grace:** Bookmarks the newest item on first check without flooding as "all new"
- **State:** Per-feed last-seen IDs persisted to disk — restart-safe
- **Graceful degradation:** Errored feeds are counted but never crash the sensor

##### Calendar Sensor

Monitors upcoming calendar events and spikes the `unfinished` drive as scheduled work approaches.

```yaml
sensors:
  calendar:
    enabled: false
    backend: "auto"               # "auto" | "macos" | "ics"
    ics_paths: []                 # paths to .ics files (for "ics" backend or "auto" fallback)
    lookahead_minutes: 120        # scan for events within 2 hours
    imminent_threshold_minutes: 30  # events within 30 min = imminent (stronger spike)
    check_interval_minutes: 5       # minimum gap between full calendar scans
    request_timeout: 5              # osascript subprocess timeout (seconds)
```

- **Drive wiring:**
  - Event in lookahead window → `unfinished.spike(0.1)`
  - Imminent event (≤ `imminent_threshold_minutes`) → `unfinished.spike(0.25)` (stronger — something is starting soon)
- **Backends:**
  - `macos` — uses `osascript` to query Apple Calendar.app; works with iCloud, Google Calendar, Exchange, CalDAV — anything Calendar.app sees
  - `ics` — parses local `.ics` files with stdlib only; cross-platform and useful for exported calendars or CI
  - `auto` — tries `macos` first, falls back to `ics` if osascript unavailable
- **Reported keys:** `events_soon`, `event_count`, `next_event_minutes`, `imminent_event`, `backend_used`
- **Note:** On non-macOS systems (Linux, Docker), set `backend: "ics"` and provide `ics_paths`

---

#### Drive Spike Reference

| Sensor | Condition | Drive | Spike |
|--------|-----------|-------|-------|
| `discord` | Channel silence | `social` | +0.15 |
| `twitter` | Mention silence | `social` | +0.10 |
| `git` | Uncommitted/untracked | `goals` | +0.15 |
| `git` | Stale push | `goals` | +0.20 |
| `git` | Commits behind | `growth` | +0.10 |
| `web` | New feed items | `curiosity` | +0.15 |
| `calendar` | Event in lookahead | `unfinished` | +0.10 |
| `calendar` | Imminent event | `unfinished` | +0.25 |

### Evaluator

```yaml
evaluator:
  mode: "rules"  # "rules" or "model"
  
  model:
    base_url: "http://localhost:11434/v1"  # Ollama, OpenAI, Groq, etc.
    api_key: "${OPENAI_API_KEY}"           # or empty string for Ollama
    model: "llama3.2:3b"                   # small, fast, cheap
    max_tokens: 100
    temperature: 0.3
    timeout_seconds: 5
```

**Rules mode (default):**
- No AI calls
- Simple threshold math: `total_pressure > trigger_threshold`
- Fast, deterministic, free

**Model mode (advanced):**
- Uses a small LLM to decide if agent should wake
- Prompt includes: drive state, sensor data, working memory
- ~500 char prompt → <$0.0001/call with llama3.2:3b
- Useful for context-aware triggering (e.g. "don't wake me for minor file changes during office hours")

**Recommended model providers:**
- **Ollama (local):** Free, private, fast. `ollama pull llama3.2:3b` and set `base_url: "http://localhost:11434/v1"`
- **Groq:** Fastest cloud option, generous free tier
- **OpenAI:** Works but overkill (and expensive) for this use case

### Workspace

```yaml
workspace:
  root: "/Users/iris/.openclaw/workspace"  # absolute path to OpenClaw workspace
  resolve_paths: true                      # make all paths relative to root
```

**Why this matters:**
- Drive sources like `"goals.json"` resolve to `{root}/goals.json`
- Portable configs: same YAML works on different machines (just update `root`)

### State

```yaml
state:
  dir: "~/.pulse"              # where to store state files
  save_interval: 300           # seconds between auto-saves (5 min)
  max_history_entries: 1000    # trigger-history.jsonl max lines
```

**State files:**
- `pulse-state.json` — current drive pressures, config overrides
- `trigger-history.jsonl` — log of all triggers (timestamp, reason, outcome)
- `mutations.json` — pending self-modification commands
- `audit-log.jsonl` — record of applied mutations

### Logging

```yaml
logging:
  level: "INFO"                # DEBUG, INFO, WARNING, ERROR
  format: "structured"         # "structured" (JSON) or "text"
  output: "stdout"             # "stdout", "file", or "/path/to/pulse.log"
  sync_to_daily_notes: true    # append trigger logs to memory/YYYY-MM-DD.md
  daily_notes_dir: "memory"    # relative to workspace root
```

**Structured logging:**
```json
{"timestamp": 1234567890, "level": "INFO", "event": "trigger", "turn": 5, "reason": "goals pressure 6.2"}
```

**Text logging:**
```
2026-02-18 00:27:13 INFO     🫀 PULSE TRIGGER #5 — reason: goals pressure 6.2
```

**Daily notes sync:**
- Appends trigger events to today's memory file
- Format: `- 00:27 ✅ Trigger #5: goals pressure 6.2 (drive: goals, pressure: 6.2)`
- Useful for agents that use daily notes as working memory

---

## Tuning for Your Use Case

### High-frequency monitoring (trading bots, alerts)
```yaml
daemon:
  loop_interval_seconds: 10  # check every 10s

drives:
  trigger_threshold: 3.0      # lower threshold
  pressure_rate: 0.05         # faster accumulation

openclaw:
  min_trigger_interval: 300   # 5 min cooldown (vs default 30 min)
  max_turns_per_hour: 20      # allow more frequent triggers
```

### Low-frequency background agent (personal assistant)
```yaml
daemon:
  loop_interval_seconds: 60   # check every minute

drives:
  trigger_threshold: 7.0       # higher threshold (fewer triggers)
  pressure_rate: 0.005         # slower accumulation

openclaw:
  min_trigger_interval: 3600   # 1 hour cooldown
  max_turns_per_hour: 5        # conservative rate limit
```

### Battery-powered device (Raspberry Pi, laptop)
```yaml
daemon:
  loop_interval_seconds: 120   # check every 2 minutes

sensors:
  filesystem:
    debounce_seconds: 10.0     # longer debounce = fewer wakeups
  system:
    check_interval_seconds: 300  # check health every 5 min

drives:
  trigger_threshold: 10.0       # very high threshold
```

---

## Environment Variables

Pulse respects these env vars:

```bash
# Override config file location
export PULSE_CONFIG=/path/to/custom-config.yaml

# Webhook token (recommended: keep out of git)
export PULSE_HOOK_TOKEN=your-secret-token

# Log level (DEBUG, INFO, WARNING, ERROR)
export PULSE_LOG_LEVEL=DEBUG

# State directory
export PULSE_STATE_DIR=~/.pulse-custom
```

---

## Config Validation

Pulse checks startup and runtime diagnostics with:

```bash
python3 -m pulse doctor
```

Checks:
- Required fields present
- Numeric ranges valid
- File paths exist
- Webhook URL reachable
- Model API accessible (if `evaluator.mode: "model"`)

---

## Dynamic Updates (Self-Modification)

The agent can change config at runtime by writing mutations:

```json
{
  "type": "adjust_threshold",
  "value": 7.5,
  "reason": "I'm getting triggered too often — raising threshold"
}
```

**Supported mutations:**
- `adjust_threshold` — change `drives.trigger_threshold`
- `adjust_rate` — change `drives.pressure_rate`
- `adjust_weight` — change a drive category's weight
- `adjust_cooldown` — change `openclaw.min_trigger_interval`
- `adjust_turns_per_hour` — change `openclaw.max_turns_per_hour`

**Guardrails:**
- Threshold: `[0.5, 50.0]`
- Rate: `[0.001, 1.0]`
- Weight: `[0.0, 5.0]`
- Cooldown: `[60, 7200]` seconds
- Max mutations: 10/hour

All mutations are logged in `audit-log.jsonl`.

---

## RL-lite Feedback Learning

Pulse includes an adaptive feedback learner that adjusts drive weights based on observed outcomes. When you send feedback via `POST /feedback` or write `turn_result.json`, the learner records the event and updates an Exponential Moving Average (EMA) per drive.

**How it works:**

| Outcome | Score |
|---------|-------|
| `success` | +1.0 |
| `partial` | +0.3 |
| `blocked` |  0.0 |
| `failure` | -1.0 |

Each feedback event updates the drive's EMA: `new_ema = α × score + (1 - α) × old_ema` (default α = 0.15).

The EMA maps to a weight multiplier in the range **[0.7, 1.3]** — drives with consistently good outcomes get up to 30% more weight; underperforming drives lose up to 30%. A floor of 0.1 prevents any drive from going completely silent.

**Monitoring:**

The `/status` endpoint includes a `learner` section:

```json
{
  "learner": {
    "drives": {
      "goals": {
        "ema": 0.4532,
        "multiplier": 1.136,
        "events": 12,
        "success_rate": 0.8333,
        "last_outcome": "success"
      }
    },
    "total_events": 12
  }
}
```

Prometheus metrics are also exposed:

| Metric | Type | Description |
|--------|------|-------------|
| `pulse_learner_ema{drive}` | gauge | EMA score [-1, 1] |
| `pulse_learner_multiplier{drive}` | gauge | Weight multiplier [0.7, 1.3] |
| `pulse_learner_events{drive}` | gauge | Events in rolling window |
| `pulse_learner_success_rate{drive}` | gauge | Fraction success+partial |

**Tuning constants** (in `src/feedback_learner.py`):

| Constant | Default | Description |
|----------|---------|-------------|
| `WINDOW` | 20 | Rolling events kept per drive |
| `ALPHA` | 0.15 | EMA learning rate |
| `MAX_ADJUSTMENT` | 0.30 | Max ±% weight shift |
| `MIN_WEIGHT_FLOOR` | 0.10 | Absolute minimum effective weight |

State persists to `<state_dir>/feedback_learner.json` and survives daemon restarts.

---

## Multi-Agent Coordination (Peer Sync)

Run multiple Pulse instances (e.g., Iris + Scout + Edge) and have them coordinate through social THALAMUS signals. Each instance polls its peers' `/status` endpoint and injects low-salience signals so agents naturally de-duplicate work and share mood state.

### Configuration

```yaml
peers:
  enabled: true
  poll_interval_seconds: 60
  peers:
    - name: scout
      url: http://192.168.1.50:9720
      token: ${SCOUT_PULSE_TOKEN}
      role: researcher
    - name: edge
      url: http://192.168.1.51:9720
      token: ${EDGE_PULSE_TOKEN}
      role: trader
```

| Field | Default | Description |
|-------|---------|-------------|
| `peers.enabled` | `false` | Enable multi-agent coordination |
| `peers.poll_interval_seconds` | `60` | Seconds between polling cycles |
| `peers[].name` | (required) | Human-readable peer name |
| `peers[].url` | (required) | Base URL of peer's health server |
| `peers[].token` | `""` | Peer's `PULSE_HOOK_TOKEN` for auth |
| `peers[].role` | `""` | Optional label (e.g. "researcher", "trader") |

### Signal Types

Peer state is injected into THALAMUS as social signals:

| Signal | Salience | Condition | Meaning |
|--------|----------|-----------|---------|
| `peer_available` | 0.10 | Peer reachable + pressure < 3.0 | Social presence, peer can take work |
| `peer_busy` | 0.20 | Peer top_pressure ≥ 3.0 | Avoid duplicating this drive's work |
| `peer_mood_shift` | 0.08 | Peer mood ≠ neutral/unknown | Gentle emotional contagion |
| `peer_offline` | 0.15 | 3+ consecutive poll failures | System awareness — peer may need help |

### Observation API

- **`GET /state/peers`** — returns peer count, reachability, and per-peer summary
- **WebSocket `/stream`** — includes `peers` in every 5-second broadcast

### Design Principles

- **Zero coupling:** reads only the public `/status` endpoint — no shared state, no new protocol
- **Fail-safe:** unreachable peers are marked stale after 120s, never crash the daemon
- **Low salience:** peer signals inform but never override local agency (all ≤ 0.20)
- **Token safety:** peer tokens are held in memory only — never persisted to `peers.json`

State persists to `<state_dir>/peers.json` and survives daemon restarts.

---

## Next Steps

- [Architecture](architecture.md) — how Pulse works
- [Deployment](deployment.md) — production setup
- [Examples](../examples/) — sample configs
