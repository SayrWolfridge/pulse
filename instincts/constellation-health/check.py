#!/usr/bin/env python3
"""Constellation Health Check — Pulse Instinct

Checks all 5 constellation agent gateways are responding.
Reports status to stdout (Pulse routes via INSTINCT.md output config).
"""

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime

# Port allocation (locked March 9, 2026)
# Each agent binds gateway port + canvas at gateway+2
AGENTS = {
    "Iris":  {"port": 18789, "emoji": "🔮"},
    "Vera":  {"port": 18790, "emoji": "⚡"},
    "Sage":  {"port": 18793, "emoji": "🌿"},
    "Mira":  {"port": 18797, "emoji": "🔨"},
    "Lyra":  {"port": 18801, "emoji": "✨"},
}

TIMEOUT_SECONDS = 5


def check_agent(name: str, port: int) -> dict:
    """Check if an agent's gateway is responding."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.getcode()
            return {"name": name, "port": port, "status": "up", "http": status}
    except urllib.error.URLError:
        pass
    except Exception:
        pass

    # Health endpoint might not exist — try root or just TCP connect
    try:
        url_root = f"http://127.0.0.1:{port}/"
        req = urllib.request.Request(url_root, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return {"name": name, "port": port, "status": "up", "http": resp.getcode()}
    except urllib.error.HTTPError as e:
        # HTTP error means server IS running (just returned 4xx/5xx)
        return {"name": name, "port": port, "status": "up", "http": e.code}
    except Exception:
        return {"name": name, "port": port, "status": "down", "http": None}


def main():
    results = []
    for name, info in AGENTS.items():
        result = check_agent(name, info["port"])
        result["emoji"] = info["emoji"]
        results.append(result)

    up = [r for r in results if r["status"] == "up"]
    down = [r for r in results if r["status"] == "down"]

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not down:
        # All healthy — brief output
        print(f"✅ Constellation healthy ({timestamp}): {len(up)}/5 agents up")
        for r in results:
            print(f"  {r['emoji']} {r['name']}: port {r['port']} ✅")
    else:
        # Something is down — alert
        print(f"⚠️ CONSTELLATION ALERT ({timestamp}): {len(down)}/5 agents DOWN")
        print()
        for r in down:
            print(f"  🔴 {r['emoji']} {r['name']}: port {r['port']} — NOT RESPONDING")
        print()
        for r in up:
            print(f"  ✅ {r['emoji']} {r['name']}: port {r['port']} — OK")
        print()
        print("Action: Check LaunchAgent status with `launchctl list | grep openclaw`")

    # Machine-readable summary for Pulse
    summary = {
        "total": len(results),
        "up": len(up),
        "down": len(down),
        "agents": {r["name"]: r["status"] for r in results},
        "alert": len(down) > 0,
    }
    print(f"\n---\n{json.dumps(summary)}")


if __name__ == "__main__":
    main()
