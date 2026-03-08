---
name: weather-market-scan
description: Scan Polymarket weather markets for FLOOR/CEILING directional opportunities during GFS windows
version: "1.0"
enabled: true

triggers:
  drives:
    curiosity: ">= 3.0"
    financial_urgency: ">= 2.0"
  context:
    gfs_window: true

cooldown_minutes: 90
timeout_seconds: 120

output:
  log: true
  discord: "edge-alerts"
  signal: false

script: scan.py
---

Scan Polymarket weather temperature markets. Focus on FLOOR and CEILING markets only.
Priority cities: NYC, London, Paris, Seattle, Chicago, Dallas.
YES if price < 0.15. NO if price > 0.45. Max $2/bet. Max 2 bets/city.
