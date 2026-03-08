#!/usr/bin/env python3
"""X engagement instinct — produce lightweight draft ideas from local context."""

import json
import os
import sys


def main():
    context = json.loads(os.environ.get("PULSE_CONTEXT", "{}"))
    body = os.environ.get("INSTINCT_BODY", "").strip()

    print("X Engagement Instinct")
    print(
        f"Context: gfs_window={context.get('gfs_window')}, hour_utc={context.get('hour_utc')}"
    )
    print("Draft prompts:")
    print("  - Share one concrete build update from today with a specific outcome.")
    print("  - Ask one narrow question that could unblock current work.")
    print("  - Turn one recent observation into a short, opinionated post.")
    if body:
        print(f"Guidance: {body.splitlines()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
