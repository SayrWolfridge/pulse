---
name: x-engagement
description: Review recent social context and prepare lightweight X engagement ideas
version: "1.0"
enabled: true

triggers:
  drives:
    social: ">= 2.0"
    curiosity: ">= 1.5"
  context:
    gfs_window: false

cooldown_minutes: 180
timeout_seconds: 60

output:
  log: true
  discord: null
  signal: false

script: engage.py
---

Review recent social context and identify 1-3 high-signal ideas worth posting or replying to on X.
Prefer concrete observations, project updates, or concise questions over generic engagement bait.
Output only draft-worthy ideas, not long essays.
