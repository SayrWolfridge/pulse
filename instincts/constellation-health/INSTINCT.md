---
name: constellation-health
description: Check all constellation agent gateways are running and report any that are down
version: "1.0"
enabled: true

triggers:
  drives:
    system: ">= 2.0"
  context:
    gfs_window: false

cooldown_minutes: 120
timeout_seconds: 30

output:
  log: true
  discord: "agent-health"
  signal: false

script: check.py
---

Monitor the constellation agent gateways (Iris, Vera, Sage, Mira, Lyra).
Checks each agent's HTTP health endpoint on its assigned port.
Reports which agents are up/down. Only alerts if something is wrong.
