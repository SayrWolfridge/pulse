# Pulse Changelog

All notable changes to Pulse will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.5] - 2026-03-09

### Added
- **GENOME v2 — Identity Bundle Export/Import** (Phase 5: Identity Portability)
  - `export_genome_v2()`: captures full identity bundle — modules, phenotype snapshot, drive pressures/weights, RL-lite learned EMA multipliers, sensor health config
  - `validate_genome_v2()`: schema validation with bounds checking on EMA [-1, 1] and multipliers [0.7, 1.3]
  - `import_genome_v2()`: restores learned weights to FeedbackLearner with merge policy (overwrite or blend)
    - `overwrite`: replace all learned EMAs with imported values (default)
    - `blend`: average imported and current EMAs element-wise (for gradual personality migration)
  - Backward-compatible: v1 genomes (no `schema_version`) still import via legacy path
  - THALAMUS `import_v2` signal on successful import
  - CLI: `pulse genome export` now outputs v2 by default; `--v1` flag for legacy format
  - CLI: `pulse genome import FILE` auto-detects v1/v2; `--merge blend` for blended import
  - 41 new tests across 8 test classes (export, identity, drives, learned weights, sensors, validation, import, round-trip)
  - Schema version: `"2.0"` — discriminator field for v1/v2 detection

### Changed
- `pulse genome export` help text updated with v2 examples
- Test suite: 1191 → 1231 passing

## [0.5.4] - 2026-03-09

### Added
- **Multi-agent Coordination — Phase 4** (`src/peer_sync.py`)
  - `PeerSync` class: polls sibling Pulse instances via their `/status` endpoint, tracks drive state, AURA, reachability, and version
  - `PeerInfo` dataclass: runtime state per peer (drives, mood, energy, focus, availability, consecutive failures)
  - THALAMUS signal injection: 4 signal types at low salience (0.08–0.20) so peers inform without overriding local agency
    - `peer_available` (salience 0.10): peer is up and not overloaded
    - `peer_busy` (salience 0.20): peer's top drive matches — social deference to avoid duplication
    - `peer_mood_shift` (salience 0.08): emotional contagion from peer mood state
    - `peer_offline` (salience 0.15): peer unreachable for 3+ consecutive polls
  - Stale detection: peer data older than 120s treated as unknown (STALE_SECONDS)
  - Atomic state persistence to `peers.json` (tokens excluded — never written to disk)
  - Module-level singleton: `init()`, `get_instance()`, `get_status()` for observation API integration
  - Wired into `NervousSystem.post_loop()`: polls peers on interval and injects signals after AURA emit
- **Config: `PeerConfig` + `PeersConfig` dataclasses** (`src/core/config.py`)
  - `peers.enabled`, `peers.poll_interval_seconds`, `peers.peers[]` (name, url, token, role)
  - Added `peers` field to `PulseConfig` — disabled by default (zero overhead when unused)
- **Observation API: `GET /state/peers`** (`src/observation_api.py`)
  - Auth-gated endpoint returning total/reachable counts + per-peer summary
  - WebSocket `/stream` now includes `peers` in every 5-second broadcast
  - File-based fallback: reads `peers.json` if module not loaded
- **Test suite: 1152 → 1191 tests** (39 new)
  - `TestPeerInfo` (3): dataclass defaults, custom construction, mutability
  - `TestPeerSyncInit` (6): empty config, peer registration, invalid peers, poll interval, should_poll
  - `TestFetchJson` (3): success, failure, invalid JSON
  - `TestPollPeer` (6): dict drives, list drives, unreachable, consecutive failures, recovery, AURA fallback
  - `TestThalamusInjection` (8): available/busy/offline/stale signals, neutral mood, salience levels, data shape, graceful no-thalamus
  - `TestSerialization` (2): file creation, token exclusion
  - `TestGetSummary` (3): empty, with peers, stale peer
  - `TestModuleSingleton` (2): pre-init status, init+get_instance
  - `TestConfigIntegration` (3): PeerConfig/PeersConfig defaults, PulseConfig.peers
  - `TestMultiPeerScenarios` (3): mixed reachability, low-failure no-offline, empty peers

### Phase 4 Status
**All four Phase 4 features now shipped:**
- ✅ Prometheus metrics (v0.5.1)
- ✅ RL-lite feedback learner (v0.5.2)
- ✅ Visual dashboard learner card (v0.5.3)
- ✅ Multi-agent coordination (v0.5.4)

## [0.5.3] - 2026-03-09

### Added
- **Dashboard Learner Card — Phase 4** (`src/observation_server.py`, `src/dashboard.html`)
  - `_get_learner_data()` helper: reads `feedback_learner.json`, computes per-drive EMA, multiplier [0.7–1.3], success rate, event count, last outcome
  - `GET /state/learner` endpoint — new auth-gated route alongside existing state subsystems (`/state/drives`, `/state/modules`, `/state/instincts`)
  - WebSocket `/stream` now includes `learner` in every 5-second real-time broadcast
  - Dashboard card: **Feedback Learner — Drive Reinforcement**
    - Per-drive bar scaled across the full [0.7, 1.3] multiplier range
    - Color-coded: green = reinforced (>1.05), red = suppressed (<0.95), purple = neutral
    - Shows +N% / -N% multiplier delta, success rate %, outcome dot per drive
    - Fetches `/state/learner` on load + every 15s; receives live updates via WebSocket
  - Closes the observability loop: the RL-lite learner introduced in v0.5.2 was invisible until now — drives that succeed or fail can be watched adapting in real time
- **Test suite: 1132 → 1152 tests** (20 new)
  - `TestStateLearner` (16 tests): endpoint auth, response schema, per-drive fields, empty-state handling
  - `TestDashboardLearner` (4 tests): card render, color logic, WebSocket payload inclusion

## [0.5.2] - 2026-03-09

### Added
- **RL-lite Feedback Learner — Phase 4** (`src/feedback_learner.py`)
  - Bandit-style adaptive learning: tracks feedback outcomes per drive in a rolling window
  - EMA (Exponential Moving Average) scoring: α=0.15 balances responsiveness with stability
  - Weight multiplier per drive: [0.7, 1.3] range — drives that consistently succeed get reinforced, underperforming drives self-correct
  - MIN_WEIGHT_FLOOR (0.1) prevents any drive from going completely silent
  - `FeedbackEvent` dataclass with outcome scoring: success (+1.0), partial (+0.3), blocked (0.0), failure (-1.0)
  - State persisted to `feedback_learner.json` with atomic writes (tmp → rename) and corrupt-file recovery
  - Wired into both feedback paths: HTTP `POST /feedback` (health.py) and file-based `turn_result.json` (daemon.py)
  - `/status` endpoint now includes `learner` section with per-drive EMA, multiplier, event count, success rate
  - Prometheus metrics: `pulse_learner_ema`, `pulse_learner_multiplier`, `pulse_learner_events`, `pulse_learner_success_rate` (all per-drive gauges)
  - `get_stats()` API: schema with per-drive EMA, multiplier, event count, success_rate, last_outcome
  - `reset_drive()`: clear history + EMA for a drive (e.g. after config change)
  - `prometheus_lines()`: standalone text-format output for custom integrations
  - 51 new tests across 10 test classes: FeedbackEvent (10), Record (8), WeightAdjustment (6), EffectiveWeight (5), Stats (6), ResetDrive (4), Persistence (5), PrometheusLines (3), Convergence (4)
- **Test suite: 1081 → 1132 tests** (51 new)

## [0.5.1] - 2026-03-09

### Added
- **Prometheus Metrics — Phase 4** (`GET /metrics` on the health port)
  - Zero external dependencies — implements Prometheus text exposition format v0.0.4 directly
  - New module `src/metrics.py` with `PulseMetrics` collector class
  - Gauges: `pulse_uptime_seconds`, `pulse_drives_pressure{drive}`, `pulse_drives_weight{drive}`
  - Counters: `pulse_triggers_total{reason}`, `pulse_trigger_failures_total{reason}`, `pulse_feedback_total{outcome}`, `pulse_turn_count_total`, `pulse_instincts_fired_total{instinct}`
  - Info: `pulse_info{version, python_version}` (build metadata)
  - `/metrics` route added to HealthServer alongside existing `/health` and `/status`
  - Daemon wired: trigger success/failure and feedback outcomes automatically recorded
  - Compatible with Prometheus, Grafana, Datadog agent, VictoriaMetrics, and any OpenMetrics scraper
  - 48 new tests covering text format helpers, collection output, counter mutations, async handler, error handling, and HealthServer integration
- **Test suite: 1033 → 1081 tests** (48 new)

## [0.5.0] - 2026-03-08

### Added
- **Pulse Instincts System** — Drive-triggered autonomous skills that fire before GENERATE
  - `instincts/` folder structure: each instinct is a self-contained folder with `INSTINCT.md` + scripts
  - YAML frontmatter spec: `triggers.drives`, `triggers.context`, `cooldown_minutes`, `timeout_seconds`, `output` routing
  - `src/instincts/` Python package: `models`, `loader`, `registry`, `executor`
  - `InstinctRegistry`: loads all instincts from config, matches against live drive state
  - `InstinctExecutor`: runs matched instinct scripts with cooldown tracking, routes output to Discord/Signal/log
  - Instincts fire deterministically before GENERATE (reliable skill → creative fallback ordering)
  - Cooldown protection per instinct — no runaway firing
  - 3 initial instincts shipped:
    - `weather-market-scan`: fires on `curiosity ≥ 3.0 + financial_urgency ≥ 2.0` during GFS windows → scans FLOOR/CEILING Polymarket opportunities
    - `memory-maintenance`: fires on `growth ≥ 2.5` during off-hours → consolidates hippocampus, prunes old memory
    - `x-engagement`: fires on `social ≥ 3.5` → drafts reply queue from X timeline
  - `instincts` config section in `pulse.yaml`
  - 39 new tests in `tests/unit/test_instincts/`
  - Full spec in `INSTINCT_SPEC.md` (617 lines)
- **Version bump: 0.4.0 → 0.5.0**

## [0.4.0] - 2026-03-08

### Added
- **`pulse doctor` — Health diagnostics CLI** — Full runtime health inspection in one command
  - Checks Python version, module imports, config file presence and validity, state/log directories, daemon process, API reachability, OpenClaw binary + gateway status
  - Clear `✓`/`✗` output per check; non-zero exit on failure for CI integration
  - `pulse doctor --json` for machine-readable output
  - `PULSE_CONFIG` env var support for non-default config locations
  - `tests/test_cli_doctor.py` — runtime path + gateway probe coverage
- **CORTEX_EXT — Learning gap detector** — Surfaces silent recurring errors automatically
  - Monitors THALAMUS broadcast stream for recurring error patterns
  - Escalates gaps that appear 3+ times to THALAMUS as `learning_gap_escalated` events
  - Startup-noise filtering: ignores events in the first 60s to avoid false positives from initialization
  - First real-world catch (same day): surfaced nephron `filter_cycle` firing 5x silently — revealed critical engram format bug
  - `tests/test_cortex_ext.py`
- **`python3 -m pulse <cmd>` routing** — Unified entry point for daemon + CLI
  - No args → daemon (unchanged)
  - Any args (`doctor`, `spike`, `drives`, etc.) → CLI subcommand
  - `python3 -m pulse --help` / `python3 -m pulse doctor` both work without config required
- **Config discovery hardening** — `PULSE_CONFIG` env var + `~` expansion in all CLI commands
- **Release tooling** in `pyproject.toml` dev extras: `build>=1.2.0`, `twine>=5.0.0`, `black>=24.0`

### Fixed
- **Nephron engram bug** — `_prune_engrams()` called `.get("memories", [])` on a raw list (3,531-entry flat list) → `AttributeError` silently swallowed on every filter cycle. Fixed: handle both `{"memories": [...]}` dict format and raw list format; use correct `timestamp` field (ms, not `ts`); use `emotion.intensity` as importance proxy.
  - 2 regression tests added: `test_engram_list_format` + `test_engram_no_attributeerror_on_list`
- **`python3 -m pulse doctor` traceback** — Running CLI subcommands via `-m pulse` always started daemon and required config. Fixed `pulse/__main__.py` to route on args presence.
- **Portable `bin/pulse`** — Removed hardcoded Homebrew path; uses `/usr/bin/env bash` + optional `PULSE_PYTHON` override
- **PyPI build** — Switched `pyproject.toml` build backend to `setuptools.build_meta`; package discovery now includes `src*` and `pulse*`; `python3 -m build` + `twine check` both clean
- **Black formatting** — Full codebase formatted; `[tool.black]` config in `pyproject.toml` (target-version py311). CI lint now stable.

### Changed
- Standardized all install/run references across docs to `pip install -e .` + `python3 -m pulse`
- `project.scripts`: `pulse` → `pulse.src.cli`; `pulse-daemon` → `pulse.src.__main__`
- Runtime paths (state/log/pid/port) respected by all CLI commands via `PULSE_CONFIG` override

### Test Counts
- v0.3.0: 787 tests
- v0.4.0: 879 tests (+92)

## [0.3.0] - 2026-02-23

### Added
- **Observation API** — HTTP API for external systems to query Pulse state in real-time
  - `GET /state` — full nervous system snapshot (all module states)
  - `GET /drives` — current drive pressures + active drives
  - `GET /health` — SPINE health report
  - `GET /mood` — ENDOCRINE mood label + hormone levels
  - `GET /dashboard` — rich text dashboard for terminal or embedding
  - Token-authenticated via `PULSE_OBS_TOKEN` env var
  - `tests/test_observation_api.py` — endpoint coverage
- **Plugin Architecture** — Drop-in extensions for Pulse's SENSE cycle
  - `pulse/src/plugin_registry.py` — `PulsePlugin` base class (sense/get_state/act/on_load/on_unload/health)
  - `PluginRegistry` singleton — register/unregister/sense_all/get_all_states/act_all
  - `discover_plugins()` — scans `~/.pulse/plugins/` for `pulse_plugin_*.py` and package entry points
  - Plugins called each `pre_sense()` cycle; failures isolated (one bad plugin can't crash the daemon)
  - `pulse plugin list/discover/health` CLI subcommands
  - `tests/test_plugin_registry.py` — 29 tests covering base class, registry ops, discovery, error isolation
- **Biosensor Integration v1** — Live biometrics from Apple Watch → nervous system
  - `pulse/src/biosensor_cache.py` — thread-safe singleton reading `biosensor-state.json` (5-min freshness check)
  - HR zone helpers: `hr_zone()`, `hrv_stress()`, `move_ring_pct()`, `sleep()`, `workout()`
  - SOMA integration: move ring close → energy +0.05; high HR → drain; workout active → posture `leaning_in`
  - ENDOCRINE integration: high HR → adrenaline +0.3; low HRV stress → cortisol -0.15 + serotonin +0.1; ring closed → dopamine +0.25; deep sleep → serotonin +0.15
  - Injected into `NervousSystem.pre_sense()` each cycle; `context["biosensor"]` available to CORTEX
  - `tests/test_biosensor_integration.py` — 21 tests
  - Setup: Cloudflare tunnel `bio.astra-hq.com → localhost:9721` + iPhone Shortcuts (see docs/BIOSENSOR_SETUP.md)
- **GENOME CLI** — Export and inspect Pulse's internal genetic fingerprint
  - `pulse genome export` — writes `~/.pulse/genome.json` (identity, drives, ENDOCRINE baseline, PLASTICITY history, CIRCADIAN profile, module weights, trait fingerprint)
  - `pulse genome traits` — human-readable trait summary (emotional range, cognitive style, social orientation, temporal pattern)
  - `pulse genome diff <genome_a> <genome_b>` — compare two genome snapshots (drift detection)
  - Feeds PHENOTYPE for consistent personality expression
- **DREAM Quality — Memory Consolidation** — CHRONICLE→ENGRAM pipeline
  - `pulse/src/memory_consolidation.py` — scores and promotes CHRONICLE events to hippocampus ENGRAM
  - `score_event()` — importance = salience × type_weight × recency_factor (24h decay to 0.3 floor)
  - `consolidate()` — deduplicates by content hash, promotes above-threshold events, decays stale ENGRAMs (>14 days × 0.8), generates `ConsolidationReport` with themes + insight text
  - Integrated into `rem.py` as Phase 6 of each dream session — runs automatically on every dream cycle
  - Solves ENGRAM staleness problem: stale patterns recycling every trigger replaced by live consolidation from CHRONICLE
  - `tests/test_memory_consolidation.py` — 24 tests

### Fixed
- **HYPOTHALAMUS count-based escalation** — Signals that fire 50+ times over 1+ hour from even a single module now escalate to active drives (persistent need pathway). Previously, multi-module threshold was the only promotion route; long-running single-source pressure could never escalate.
  - `age_hours = (now - pending["first_seen"]) / 3600`
  - `count_escalation = pending["count"] >= 50 and age_hours >= 1.0`
  - Threshold check: `(len(pending["modules"]) >= threshold or count_escalation) and need_name not in state["active_drives"]`

### Test Counts
- v0.2.5: 693 tests
- v0.3.0: 787 tests (+94)

## [0.2.5] - 2026-02-22

### Added
- **PARIETAL — World Model Module**: Environment discovery, health signal inference, and dynamic sensor registration
  - `scan()` walks workspace up to 3 levels deep, detects project types (Python, Node, trading bot, Cloudflare worker, Fly.io app, Go, Rust, Docker)
  - `_infer_signals()` generates health signals from heuristics: log file watchers, HTTP health endpoints, git status, trade activity monitors
  - `register_sensors()` dynamically registers `ParietalFileSensor`, `ParietalFileContentSensor`, `ParietalHttpSensor`, `ParietalGitSensor` with SensorManager at runtime
  - `update_signal_weight()` integrates with PLASTICITY feedback — actionable signals gain weight, noise signals lose weight
  - `get_context()` provides compact world model summary for CORTEX context injection
  - Extracts goal conditions from PROJECTS.md / TIERS.md / GOALS.md checkboxes
  - Extracts deployment URLs from wrangler.toml, fly.toml, .env files
  - State persisted to `parietal-state.json` with full signal weight history
- `SensorManager.add_sensor()` — dynamic sensor registration at runtime
- `ParietalConfig` dataclass in `core/config.py` with `parietal:` YAML section
- PARIETAL integrated into `NervousSystem` (init, warm-up, post_loop re-scan, startup, shutdown)
- PARIETAL context injected into daemon trigger messages (unhealthy systems, pending goals)
- Initial world model scan + sensor registration at daemon startup
- `tests/test_parietal.py` — 45 tests covering discovery, signal inference, file age sensors, git sensors, weight updates, context output, re-scan deduplication, state isolation, goal conditions, serialization, sensor registration, HTTP sensors, caps, and status
- Test count: 648 → 693 passing

## [0.2.4] - 2026-02-22

### Fixed
- **Gap #1 — EXCEPTION rule false positive**: Model evaluator's EXCEPTION rule fired on ambient floor-level drives (total > 10.0 but every individual drive ~1.24). Added guard: highest individual drive must exceed 1.5 before EXCEPTION triggers.
- **Gap #3 — Daily notes file locking**: All 4 daily-note write sites (daily_sync log_trigger, log_mutation; daemon _maybe_generate; health _handle_feedback) now use `fcntl.flock()` for exclusive locking. Prevents duplicate/corrupted entries under concurrent writes.

### Changed
- **Gap #2 — State directory isolation**: All 33 nervous system modules renamed `STATE_DIR` → `_DEFAULT_STATE_DIR` (and derived file constants). `NervousSystem.__init__()` now accepts `state_dir: Optional[Path]` parameter, patching each module's paths at init time. Enables multi-companion isolation without importlib.reload hacks.
  - `pulse-api/main.py` now passes `state_dir=companion_state_dir` directly instead of reloading all Pulse modules per companion
  - `cli.py` constant renamed for consistency
  - 27 test files updated to reference new constant names

### Added
- `tests/test_evaluator_model.py` — 6 tests for EXCEPTION rule guard
- `tests/test_daemon_logging.py` — 5 tests for flock presence and concurrent write safety
- `tests/test_state_isolation.py` — 8 tests for multi-companion state directory isolation

## [0.2.3] - 2026-02-18

### Changed
- **Work Discovery Enhancement**: Iris integration now injects comprehensive context into isolated sessions when goals are blocked
  - Loads TIERS.md (full project roadmap) to identify alternative work streams
  - Loads recent memory (today + yesterday) for situational awareness
  - Runs hippocampus recall for pattern-based work suggestions
  - Loads working memory threads for continuity
  - Adds explicit instruction: "DO NOT just report 'standing by' — find NEW productive work"
- **Behavioral Improvement**: Isolated sessions now consistently find autonomous work instead of defaulting to status reports when collaborative tasks are blocked
- **Context Limits**: Added character limits per section (TIERS: 2000, memory: 1500, hippocampus: 1000, working memory: 500) to prevent token bloat while maintaining utility

### Fixed
- Work discovery context was implemented in v0.2.1 but not consistently producing autonomous action
- Added stronger directive language to prevent "blocked, standing by" default behavior

## [0.2.2] - 2026-02-17

### Added
- **High-Pressure Override**: Daemon now forces trigger if pressure > 10.0 and idle > 30 minutes, bypassing model evaluator entirely (belt-and-suspenders approach)
- **Sonnet 4.5 Support**: Isolated sessions now use `anthropic/claude-sonnet-4-5` by default (saves Opus budget for main conversations)
- Model-based evaluator configuration in pulse.yaml with Ollama as default backend

### Fixed
- **Conversation Sensor**: Was falsely detecting cron/hook sessions as "human conversation" by checking mtime of ANY .jsonl file
  - Now only checks main session file (largest .jsonl > 100KB) for accurate conversation detection
- **Model Evaluator**: llama3.2:3b was returning "no trigger" even at pressure 24.7+ due to unclear suppression logic
  - High-pressure override ensures triggers happen when truly needed

### Changed
- Isolated session model default: `opus` → `sonnet` (cost optimization)
- Required Sonnet 4.5 to be added to OpenClaw gateway config (`allowed_models`)

## [0.2.1] - 2026-02-17

### Added
- **Isolated Session Mode**: Pulse triggers now spawn separate hook sessions instead of injecting into main conversation
  - Configured via `session_mode: "isolated"` in pulse.yaml
  - Prevents interrupting human conversations
  - Results announced back to Signal when `deliver: true`
- **Iris Integration**: Custom integration module connecting Pulse to CORTEX.md cognitive loop
  - Loads working memory snapshot for cross-session continuity
  - Provides hippocampus recall for pattern-based context
  - Injects OPERATIONS.md/CORTEX.md loop instructions
  - Discord #pulse-log audit trail integration
- **Webhook Enhancements**: webhook.py updated to pass `isolated: true` flag to OpenClaw hooks endpoint
- **Session Context**: Working memory, recent goals, and cognitive state included in isolated session triggers

### Changed
- Default session mode: `main` → `isolated` (cleaner separation of autonomous work)
- Webhook delivery now includes model override for isolated sessions

## [0.2.0] - 2026-02-17

### Added
- **Feedback Endpoint**: POST /feedback on health server (port 9720) for drive decay after successful work
  - Accepts JSON: `{"drives_addressed": ["drive"], "outcome": "success", "summary": "what I did"}`
  - Drives decay by 70% when addressed, reinforcing productive loops
- **Two-Layer Architecture**: Lightweight daemon (no AI calls) + full agent turns via webhook
  - Daemon monitors state, accumulates pressure, detects urgency
  - Agent does the work, sends feedback, drives decay
  - Clear separation of concerns
- **Conversation Suppression**: Detects active human chat by checking main session file mtime
  - Suppresses triggers during conversation (configurable cooldown)
  - Prevents Pulse from interrupting collaborative work
- **Model-Based Evaluator**: Optional context-aware triggering via local LLM (Ollama llama3.2:3b)
  - Smarter than rules-based, still zero vendor lock-in
  - Configurable via `evaluator.mode: "model"` in pulse.yaml

### Fixed
- Drive pressure accumulation now based on time since last addressed (prevents stale trigger loops)
- Conversation sensor accuracy improved (checks largest session file only, not all .jsonl)
- Feedback loop validated with real autonomous sessions (9+ successful cycles on Feb 17)

### Changed
- Health endpoint moved from port 18788 → 9720 (clearer separation from OpenClaw)
- Daemon startup requires sourcing `~/.pulse/.env` for PULSE_HOOK_TOKEN (via `pulse/bin/run.sh`)

## [0.1.0] - 2026-02-15

### Added
- Initial Pulse daemon architecture
- Drive engine with 6 categories (goals, curiosity, emotions, unfinished, social, growth)
- Filesystem sensor (watches workspace for changes)
- System sensor (monitors health metrics)
- Conversation sensor (detects human activity)
- Rules-based priority evaluator
- State persistence (pulse-state.json)
- Webhook integration with OpenClaw
- Health endpoint (GET /health, GET /status)
- Configuration via YAML (pulse.yaml)
- Documentation (architecture, configuration, deployment guides)
- Example configs (personal-assistant.yaml, trading-bot.yaml)
- ClawHub listing draft
- MIT license (open source)

[Unreleased]: https://github.com/astra-ventures/pulse/compare/v0.2.5...HEAD
[0.2.5]: https://github.com/astra-ventures/pulse/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/astra-ventures/pulse/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/astra-ventures/pulse/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/astra-ventures/pulse/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/astra-ventures/pulse/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/astra-ventures/pulse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/astra-ventures/pulse/releases/tag/v0.1.0

### Improvement Candidate (Feb 22, 2026)
**Blocker-aware drive suppression**

Pattern observed: model-generated trigger focus re-suggests the same blocked items within a 30-min window, creating wasteful repetitive loops. Goals drive stays elevated even after a complete sweep because "blocked" != "resolved."

Proposed fix: Add `blocker_last_checked` timestamps to drive state. When a specific focus item has been verified-blocked within the last N minutes (configurable, default 30), suppress re-triggering that focus until either:
1. Status changes (external signal), OR
2. The cooldown window expires

This would reduce wasted trigger sessions on persistent blockers and let the drive naturally decay without manufactured "sweeps."

File under: HYPOTHALAMUS / drive evolution / blocker awareness
