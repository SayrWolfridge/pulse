#!/bin/bash
set -a; source ~/.pulse/.env; set +a
cd /Users/iris/.openclaw/workspace/pulse
export PYTHONPATH=/Users/iris/.openclaw/workspace
exec /Users/iris/.openclaw/workspace/pulse/venv/bin/python3 -m pulse.src.runtime
