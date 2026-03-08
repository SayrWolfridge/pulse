---
name: memory-maintenance
description: Consolidate and review daily memory files when idle and drives are low
version: "1.0"
enabled: true

triggers:
  drives:
    curiosity: ">= 1.0"
  context:
    gfs_window: false

cooldown_minutes: 360
timeout_seconds: 120

output:
  log: true
  discord: null
  signal: false

script: consolidate.py
---

Review recent daily memory files and summarize key events.
Look for patterns, decisions, and lessons worth capturing long-term.
Output a brief summary of what was found.
