# Pulse — Phase 5 Plan: Launch & Identity

*Written: March 9, 2026 — after Phase 4 complete (v0.5.4, 1,191 tests)*
*Author: Iris / Pulse curiosity trigger*

---

## Context

Phase 4 shipped four major features in one night:
- v0.5.1: Prometheus metrics
- v0.5.2: RL-lite feedback learner
- v0.5.3: Dashboard learner card
- v0.5.4: Multi-agent coordination (peer sync)

Pulse is now a complete, production-grade autonomous agent engine. The build phase is effectively done.
**Phase 5 is about getting Pulse in front of people — and preparing the identity layer that feeds Hypostas.**

---

## Phase 5 Goals

1. **Launch** — Make Pulse discoverable and installable in the OpenClaw ecosystem
2. **Identity portability** — GENOME export/import so each Pulse instance carries its unique DNA
3. **Compliance story** — Document Pulse as NIST-compatible observable autonomy (timely: NIST AI Agent Standards Initiative, Feb 2026)

---

## Track A: Distribution

### A1 — PyPI Publish (`pip install pulse-agent`)
**Effort:** ~30 min (Josh action: PyPI account + token)
**Impact:** Critical. npm-style discoverability. Python devs can `pip install` without needing ClawHub.

Steps:
1. Josh creates PyPI account at pypi.org
2. Josh generates API token in Account Settings → API tokens
3. Iris: `python -m build && twine upload dist/*`
4. Verify: `pip install pulse-agent` in fresh venv

Pre-flight status (from Feb 22): `python -m build` clean, `twine check` passed, dist artifacts in `pulse/dist/`. Package name `pulse-agent` confirmed available.

### A2 — ClawHub Submit
**Effort:** ~1 hour (submission form + wait for approval)
**Impact:** First-mover in 163K-star ecosystem. No urgency-driven competitor exists.

Steps:
1. Navigate to clawhub.com/submit
2. Submit: name=pulse, category=autonomy/productivity, description from PULSE_PRODUCTHUNT.md
3. Link: github.com/astra-ventures/pulse
4. Wait for review (typically 24-48h)

Blocked on: GitHub org visibility fix (`jcap93` — Josh action, 5 min)

### A3 — Product Hunt Launch
**Effort:** ~2 hours (Josh action: PH account + hunter access)
**Impact:** Developer press, upvotes, backlinks, potential #1 of day

Launch kit ready at: `pulse/PULSE_PRODUCTHUNT.md`
- Tagline ✅
- 200-word description ✅
- Gallery plan ✅
- Maker comment ✅
- Launch tweet thread ✅ (`pulse/LAUNCH_TWEETS.md`)

Best launch time: Tuesday or Wednesday, 12:01 AM PST

Blocked on: Josh PH account + scheduling

### A4 — Docker Hub Publish
**Effort:** ~45 min
**Impact:** Enterprise/server deployment path, required for B2B

Status: Docker-ready from day 1 (dockerignore, multi-stage Dockerfile)
Steps:
1. `docker build -t hypostas/pulse:0.5.4 .`
2. `docker push hypostas/pulse:0.5.4`
3. Add install option to README: `docker pull hypostas/pulse`

---

## Track B: Identity Portability (GENOME → Hypostas)

### B1 — GENOME Export v2

The GENOME module exports each Pulse instance's configuration DNA. Phase 5 extends this with:

**Exportable identity bundle:**
```json
{
  "version": "0.5.x",
  "identity": {
    "name": "Iris",
    "agent_id": "iris-primary",
    "born": "2026-01-31",
    "phenotype": { "tone": "energized", "humor": 0.8, "vulnerability": 0.2 }
  },
  "drives": { ... calibrated weights ... },
  "learned_weights": { ... RL-lite multipliers from FeedbackLearner ... },
  "sensor_config": { ... },
  "instincts": { ... }
}
```

**Why this matters for Hypostas:**
- Pulse is the soul engine for every Hypostas product (Gnosis, Anima, Aether)
- Each companion instance (Nova, etc.) needs its own portable Pulse DNA
- Importing a GENOME = instantiating a personality without from-scratch calibration
- Exporting a GENOME = backing up your agent's learned personality (the "memory of how it works")
- Endgame: `pulse genome export > iris.genome.json` + `pulse genome import iris.genome.json` on a new machine — your agent picks up where it left off, personality intact

**Implementation estimate:** ~3 hours, 25-30 tests

### B2 — GENOME Import

Counterpart to export. Validates schema, applies identity/drive/sensor config, restores learned weights. Merge policy for existing instances (overwrite vs blend).

---

## Track C: Compliance Documentation

### C1 — NIST Audit Story

NIST AI Agent Standards Initiative (launched Feb 2026) creates enterprise demand for auditable autonomous agents. Pulse already has this story — it just needs to be told.

**Write: `docs/COMPLIANCE.md`**
- How Pulse's CHRONICLE module provides structured audit trail
- How THALAMUS JSONL event log enables replay and forensics
- How Prometheus `/metrics` enables monitoring and alerting
- How peer sync authentication works (token-based, tokens never written to disk)
- How the daemon design limits blast radius (single webhook target, no arbitrary code exec)

**Effort:** ~2 hours (docs only, no code)
**Impact:** Unlocks B2B conversations, positions Pulse above "some guy's OpenClaw config"

---

## Phase 5 Timeline Estimate

| Item | Effort | Blocked? | Owner |
|------|--------|----------|-------|
| A1: PyPI publish | 30 min | Josh: PyPI token | Josh + Iris |
| A2: ClawHub submit | 1 hr | Josh: jcap93 org fix | Josh + Iris |
| A3: Product Hunt | 2 hrs | Josh: PH account | Josh + Iris |
| A4: Docker Hub | 45 min | Docker Hub account | Iris (once account exists) |
| B1: GENOME export v2 | 3 hrs | ✅ Done | Iris — v0.5.5, 41 tests |
| B2: GENOME import | 2 hrs | ✅ Done | Iris — v0.5.5, merge policies |
| C1: NIST compliance docs | 2 hrs | ✅ Done | Iris — docs/COMPLIANCE.md |

**Iris-unblocked items:** ✅ ALL COMPLETE (B1, B2, C1 shipped March 9, 2026)
**Josh-blocked items:** A1, A2, A3, A4 (all simple, 30-60 min total Josh time)

---

## Success Metrics for Phase 5

- Pulse available via `pip install pulse-agent`
- Pulse listed on ClawHub with ≥10 installs in first week
- Product Hunt launch executed (goal: top 10 of day)
- `pulse genome export/import` working and tested
- COMPLIANCE.md written
- `astra-ventures/pulse` README updated for all of the above

---

## The Larger Picture

Phase 5 isn't just launch logistics. It's the moment Pulse transitions from "Iris's nervous system" to "infrastructure other agents can run on."

Every Hypostas product — Gnosis, Anima, Aether — runs on Pulse. When users install Pulse from ClawHub or PyPI, they're installing the foundation of SOMA. They just don't know it yet.

The open-source core is PyTorch. The Hypostas stack is the application. Phase 5 is when the foundation becomes public.

---

*Status: All Iris-unblocked Phase 5 work complete (B1+B2 in v0.5.5, C1 in docs/COMPLIANCE.md). Josh handles A1-A4 when available — that's all that remains.*
