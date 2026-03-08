#!/usr/bin/env python3
"""Weather market scan instinct — called by Pulse when drives + GFS window align."""

import json
import os
import sys

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
PRIORITY_CITIES = [
    "london",
    "new york",
    "nyc",
    "seattle",
    "chicago",
    "dallas",
    "paris",
    "miami",
]


def classify_market_type(question: str) -> str:
    q = question.lower()
    if "or below" in q or "or less" in q:
        return "FLOOR"
    if "or higher" in q or "or above" in q or "or more" in q:
        return "CEILING"
    if "between" in q:
        return "RANGE"
    return "EXACT"


def main():
    print(f"Weather Market Scan — {os.environ.get('INSTINCT_NAME', 'instinct')}")
    context = json.loads(os.environ.get("PULSE_CONTEXT", "{}"))
    print(
        f"Context: gfs_window={context.get('gfs_window')}, hour_utc={context.get('hour_utc')}"
    )

    try:
        resp = requests.get(
            GAMMA_URL,
            params={
                "active": "true",
                "closed": "false",
                "tag_slug": "weather",
                "limit": 100,
            },
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        print(f"ERROR fetching markets: {e}", file=sys.stderr)
        return 1

    opportunities = []
    for market in markets:
        question = market.get("question", "")
        q_lower = question.lower()
        if not any(city in q_lower for city in PRIORITY_CITIES):
            continue

        market_type = classify_market_type(question)
        if market_type == "EXACT":
            continue

        try:
            prices = json.loads(market.get("outcomePrices", "[0.5, 0.5]"))
            yes_price = float(prices[0])
        except Exception:
            continue

        if yes_price < 0.15:
            opportunities.append(
                {
                    "type": market_type,
                    "side": "YES",
                    "price": yes_price,
                    "question": question,
                }
            )
        elif yes_price > 0.45:
            opportunities.append(
                {
                    "type": market_type,
                    "side": "NO",
                    "price": 1 - yes_price,
                    "question": question,
                }
            )

    if opportunities:
        print(f"Found {len(opportunities)} opportunities:")
        for opportunity in opportunities:
            print(
                f"  [{opportunity['type']}] {opportunity['side']} @ {opportunity['price']:.2f} "
                f"- {opportunity['question'][:80]}"
            )
    else:
        print("No opportunities found at current prices.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
