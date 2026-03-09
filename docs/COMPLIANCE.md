# Pulse — Observable Autonomy & Compliance

*Written: March 9, 2026*
*Relevant standards: NIST AI RMF (2023), NIST AI Agent Standards Initiative (Feb 2026)*

---

## Overview

Pulse is designed from the ground up for **observable autonomy** — an agent that acts on its own initiative but never in a black box. Every drive, every trigger decision, every outcome is logged, measurable, and controllable by the operator.

This document maps Pulse's architecture to the NIST AI Risk Management Framework (AI RMF) and describes how each component supports enterprise compliance requirements.

---

## NIST AI RMF Alignment

The NIST AI RMF defines four core functions: **GOVERN**, **MAP**, **MEASURE**, and **MANAGE**. Pulse implements each.

---

### GOVERN — Policies, Culture, and Accountability

> *"Policies, processes, procedures, and practices across the organization related to the mapping, measuring, and managing of AI risks are in place, transparent, and implemented effectively."*

**How Pulse implements it:**

**Configuration-as-governance.** All Pulse behavior is defined in a single YAML file (`pulse.yaml`). No code changes are required to adjust drive weights, trigger thresholds, sensor polling intervals, or allowed behaviors. Governance is expressed as configuration, not deployment.

```yaml
drives:
  curiosity:
    base_weight: 1.0      # Auditable — what weight does this drive carry?
    max_pressure: 15.0    # Hard ceiling — how aggressive can it get?

triggers:
  threshold: 2.5          # When does the agent self-activate?
  cooldown_seconds: 1800  # Hard floor — minimum time between triggers
  daily_limit: 24         # Hard ceiling — max activations per 24h
```

**Human-legible reasoning.** Every trigger includes a machine-readable reason string (`single_drive_threshold`, `multi_drive_convergence`, `high_pressure_override`) that describes *why* the agent activated. Operators can audit every decision.

**Open source.** Pulse's complete source is published at `github.com/astra-ventures/pulse` under MIT license. Security teams can audit the exact code running in their environment. No black boxes, no cloud calls, no telemetry.

---

### MAP — Categorization, Context, and Risk Assessment

> *"The AI system's expected context of use, assumptions, limitations, and AI risk categories are identified, categorized, and communicated."*

**How Pulse implements it:**

**Explicit drive taxonomy.** Pulse uses a defined set of named drives (`goals`, `curiosity`, `social`, `system`, `growth`, `emotions`, `unfinished`) with documented semantics. Operators know exactly what motivates agent behavior and can configure weights accordingly.

**Sensor scope declaration.** Every data source Pulse reads is declared in `pulse.yaml`. The agent cannot observe data sources not listed in configuration. Supported sensors:
- `calendar` — event density from connected calendar APIs
- `git` — uncommitted changes, branch staleness
- `system` — CPU/memory pressure, disk usage
- `web` / `rss` — external content changes (opt-in, URL-specific)
- `discord` / `signal` / `telegram` — message volume (no content)
- `twitter` — mention/engagement rate

No sensor reads data outside its declared scope. No prompt injection from sensor data (sensors emit structured numeric signals, not text instructions).

**Peer isolation.** Multi-agent coordination (`peer_sync`) operates via authenticated polling of sibling `/status` endpoints only. Peers cannot push instructions to each other — signals flow through THALAMUS at intentionally low salience (0.08–0.20), making peer influence advisory, not directive.

---

### MEASURE — Analysis, Assessment, and Monitoring

> *"The AI system's performance and impacts are analyzed and assessed using a range of quantitative and qualitative approaches."*

**How Pulse implements it:**

**Prometheus metrics endpoint.** `GET /metrics` on the health port (default: 8765) returns standard Prometheus text format for scraping by Grafana, Datadog, or any compatible monitoring stack. Zero external dependencies required.

Key metrics exposed:

| Metric | Type | Description |
|--------|------|-------------|
| `pulse_drives_pressure{drive}` | Gauge | Real-time pressure per drive (0–15) |
| `pulse_drives_weight{drive}` | Gauge | Configured base weight per drive |
| `pulse_triggers_total{reason}` | Counter | Trigger count by reason (cumulative) |
| `pulse_trigger_failures_total{reason}` | Counter | Failed triggers by reason |
| `pulse_feedback_total{outcome}` | Counter | Feedback outcomes (success/partial/blocked/failure) |
| `pulse_instincts_fired_total{instinct}` | Counter | Autonomous skill activations |
| `pulse_uptime_seconds` | Gauge | Daemon uptime |
| `pulse_turn_count_total` | Counter | Total agent turns initiated |

**RL-lite drive reinforcement.** The feedback learner (`src/feedback_learner.py`) tracks outcome history per drive using a 20-event rolling window with EMA weighting (α=0.15). Multipliers range [0.7, 1.3] — drives producing poor outcomes are automatically suppressed; drives producing good outcomes are reinforced. All learner state is visible at `GET /state/learner`.

**Observable dashboard.** A built-in web UI (default: localhost:8765) shows live drive pressures, trigger history, learner multipliers, module health, and peer state. No external services required.

**WebSocket streaming.** `ws://localhost:8765/stream` emits agent state every 5 seconds — integrates directly into custom monitoring or compliance dashboards.

---

### MANAGE — Response, Recovery, and Improvement

> *"AI risks based on assessments and other input are prioritized, responded to, and managed."*

**How Pulse implements it:**

**Human control mechanisms:**

| Control | How to use |
|---------|------------|
| **Pause all triggers** | Set `triggers.threshold: 99.0` in config — no drive will naturally reach 99 |
| **Hard stop** | `kill -SIGTERM <pulse-pid>` — daemon shuts down gracefully |
| **Rate limit** | `triggers.daily_limit: N` — hard ceiling on daily activations |
| **Per-drive suppression** | Set `drives.<name>.base_weight: 0.0` — drive will not accumulate pressure |
| **Cooldown floor** | `triggers.cooldown_seconds: N` — minimum time between any two triggers |

**Audit trail.** Every trigger is logged to `turn_result.json` with: timestamp, drive pressures at activation, trigger reason, agent session ID, and outcome. Full history is queryable via `GET /state`.

**Feedback loop integrity.** The feedback endpoint (`POST /feedback`) requires authenticated requests (bearer token). Agents cannot self-reinforce without authorization. The learner rejects malformed feedback events gracefully.

**Rollback.** Pulse state is file-based (`pulse-state.json`, `feedback_learner.json`). Restoring a prior state is as simple as replacing files and restarting the daemon. No database migrations required.

**Multi-instance isolation.** Each Pulse instance has its own configuration file, state directory, and port binding. Running multiple Pulse instances (for multiple agents) does not create shared state or cross-contamination risk.

---

## Security Properties

**No outbound network calls.** Pulse does not call home, report telemetry, or make network requests beyond what the operator explicitly configures in `pulse.yaml`. The daemon runs entirely locally.

**Credential handling.** API tokens (for peer sync, sensor integrations) are stored in environment variables or configuration only. Tokens are explicitly excluded from `peers.json` state persistence (see `TestSerialization` in `tests/test_peer_sync.py`).

**Prompt injection surface.** Sensors emit numeric signals, not natural language. A malicious RSS feed cannot inject text instructions into Pulse's decision loop — the RSS sensor produces a float (content change rate), not a prompt.

**Dependency surface.** Pulse's core has **zero external Python dependencies** beyond the standard library. Optional integrations (`aiohttp` for health server, `watchdog` for file sensors) are optional. Minimal dependency surface = minimal supply chain risk.

**Process isolation.** Pulse runs as a separate process from the AI agent it drives. Compromising the agent does not give access to Pulse's configuration or state directly.

---

## NIST AI Agent Standards Initiative (Feb 2026)

The NIST AI Agent Standards Initiative identified six key properties for trustworthy autonomous AI systems. Pulse's alignment:

| NIST Property | Pulse Implementation |
|--------------|---------------------|
| **Transparency** | All drives, sensors, and thresholds are human-readable config. Every trigger includes a legible reason string. Full source published under MIT. |
| **Explainability** | `GET /state` returns complete agent state in JSON at any moment. Dashboard visualizes decision state in real time. |
| **Auditability** | `turn_result.json` logs every trigger with context. Prometheus metrics provide time-series audit history. |
| **Human Oversight** | Configurable thresholds, rate limits, cooldowns, and per-drive weight suppression. SIGTERM for hard stop. |
| **Robustness** | Sensors degrade gracefully — a failed sensor does not crash the daemon or trigger spuriously. Feedback learner recovers from corrupt state files. |
| **Containment** | No outbound calls beyond declared sensors. Peer signals are advisory-only (low salience). No agent-to-agent instruction passing. |

---

## Deployment Guidance for Compliance Teams

**For regulated environments (finance, healthcare, legal):**
1. Set `triggers.daily_limit` to a conservative value (e.g., 12 triggers/day)
2. Set `triggers.cooldown_seconds: 3600` (one trigger per hour maximum)
3. Configure Prometheus scraping → Grafana for audit dashboards
4. Disable sensors that touch external data if not required
5. Review `pulse.yaml` as part of your standard configuration audit process

**For SOC 2 Type II:**
- Pulse state files constitute the audit log for agent autonomous actions
- Prometheus metrics support continuous control monitoring requirements
- All configuration changes require file system write access (OS-level access control applies)

**For internal security review:**
- Source: `github.com/astra-ventures/pulse` (MIT, fully open)
- Dependency audit: `pip-audit` / `safety check` on minimal dependency set
- Network audit: Pulse makes no outbound connections beyond configured sensor URLs

---

## Summary

Pulse treats autonomous agency as an engineering problem with a compliance solution: if you can measure it, you can manage it. If you can configure it, you can govern it. If you can log it, you can audit it.

The goal is not to limit what an agent can do — it's to ensure that what it does is always observable, explainable, and controllable. Autonomy without oversight isn't a feature. Observable autonomy is.

---

*Questions? Open an issue at github.com/astra-ventures/pulse or see [configuration.md](./configuration.md) for full config reference.*
