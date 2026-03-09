"""
Web Sensor — Phase 3 Integration

Monitors a list of RSS/Atom feed URLs for new content and reports
whether fresh items have appeared since the last read cycle.

New content → ``curiosity`` drive spike.

Architecture:
  - Parses RSS 2.0 and Atom 1.0 with Python stdlib ``xml.etree.ElementTree``
    (zero extra dependencies).
  - Tracks the most-recent item identifier (``<guid>`` / ``<id>`` / ``<link>``)
    and publication timestamp per feed across restarts via a JSON state file.
  - Respects a per-sensor ``check_interval_minutes`` so fast Pulse cycles
    don't hammer feed servers.
  - Graceful degradation: a feed that errors is counted but never crashes the
    sensor; the next cycle will retry.

Config (pulse.yaml):
  sensors:
    web:
      enabled: true
      feeds:
        - https://hnrss.org/frontpage           # Hacker News front page
        - https://feeds.feedburner.com/TechCrunch
        - https://blog.langchain.dev/rss/
      check_interval_minutes: 30   # minimum gap between polls per feed
      max_items_per_feed: 20       # cap items inspected (oldest feeds can be huge)
      request_timeout: 10          # HTTP timeout per feed (seconds)

Reported keys:
  new_content       bool   — True if ≥1 feed has unseen items
  new_items_count   int    — total new items across all feeds
  feeds_checked     int    — feeds successfully fetched this cycle
  feeds_errored     int    — feeds that failed (network / parse)
  feeds_skipped     int    — feeds inside their check-interval window
  timestamp         float  — epoch of this read
"""

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import BaseSensor

logger = logging.getLogger("pulse.sensors.web")

# XML namespaces used by Atom feeds
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _extract_rss_items(root: ET.Element, max_items: int) -> List[Dict[str, str]]:
    """Pull items from an RSS 2.0 document."""
    items = []
    channel = root.find("channel")
    if channel is None:
        return items
    for item in channel.findall("item")[:max_items]:
        uid = (
            (item.findtext("guid") or "").strip()
            or (item.findtext("link") or "").strip()
        )
        pub = (item.findtext("pubDate") or "").strip()
        items.append({"uid": uid, "pub": pub})
    return items


def _extract_atom_items(root: ET.Element, max_items: int) -> List[Dict[str, str]]:
    """Pull entries from an Atom 1.0 document."""
    items = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:max_items]:
        uid = (
            (entry.findtext("atom:id", namespaces=_ATOM_NS) or "").strip()
            or (entry.find("atom:link", _ATOM_NS) or {}).get("href", "").strip()  # type: ignore[call-overload]
        )
        pub = (
            entry.findtext("atom:updated", namespaces=_ATOM_NS)
            or entry.findtext("atom:published", namespaces=_ATOM_NS)
            or ""
        ).strip()
        items.append({"uid": uid, "pub": pub})
    return items


def _parse_feed(xml_bytes: bytes, max_items: int) -> List[Dict[str, str]]:
    """Parse RSS or Atom XML; return list of {uid, pub} dicts."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error: {exc}") from exc

    tag = root.tag.lower()
    if "rss" in tag or root.find("channel") is not None:
        return _extract_rss_items(root, max_items)
    if "feed" in tag or root.find("atom:entry", _ATOM_NS) is not None:
        return _extract_atom_items(root, max_items)
    raise ValueError("Unrecognised feed format (not RSS 2.0 or Atom 1.0)")


# ---------------------------------------------------------------------------
# Per-feed state
# ---------------------------------------------------------------------------

@dataclass
class _FeedState:
    last_seen_uid: str = ""    # uid of the most-recently observed item
    last_checked: float = 0.0  # epoch when we last polled this feed


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

class WebSensor(BaseSensor):
    """Poll RSS/Atom feeds and report when new content appears.

    Feeds:
      - ``web.new_content``     → ``curiosity`` drive spike when unseen items found
    """

    name = "web"

    def __init__(self, config: PulseConfig):
        self.config = config
        self.web_cfg = config.sensors.web  # type: ignore[attr-defined]
        self._feed_states: Dict[str, _FeedState] = {}
        self._state_file: Optional[Path] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Set up state tracking and load persisted feed positions."""
        state_dir = Path(self.config.state.dir).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = state_dir / "web_sensor_state.json"

        feeds = self.web_cfg.feeds
        if not feeds:
            logger.warning("Web sensor: no feeds configured")
            return

        # Seed per-feed state entries
        for url in feeds:
            self._feed_states[url] = _FeedState()

        # Restore last-seen positions from disk
        self._load_state()

        logger.info(
            "Web sensor initialised — monitoring %d feed(s): %s",
            len(feeds),
            ", ".join(feeds),
        )

    async def stop(self) -> None:
        """Persist state on graceful shutdown."""
        self._save_state()

    # ------------------------------------------------------------------
    # Main read
    # ------------------------------------------------------------------

    async def read(self) -> dict:
        """Poll due feeds; return aggregated new-content report."""
        if not self._feed_states:
            return self._empty_result()

        now = time.time()
        interval_sec = self.web_cfg.check_interval_minutes * 60
        max_items = self.web_cfg.max_items_per_feed
        timeout = self.web_cfg.request_timeout

        # Which feeds are due for a check?
        due = [
            url for url, state in self._feed_states.items()
            if (now - state.last_checked) >= interval_sec
        ]
        skipped = len(self._feed_states) - len(due)

        if not due:
            logger.debug("Web sensor: all feeds inside check-interval, skipping poll")
            return {
                "new_content": False,
                "new_items_count": 0,
                "feeds_checked": 0,
                "feeds_errored": 0,
                "feeds_skipped": skipped,
                "timestamp": now,
            }

        # Poll due feeds concurrently
        tasks = {url: self._poll_feed(url, max_items, timeout) for url in due}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        feed_results = dict(zip(tasks.keys(), results))

        total_new = 0
        checked = 0
        errored = 0

        for url, result in feed_results.items():
            state = self._feed_states[url]
            state.last_checked = now

            if isinstance(result, Exception):
                logger.warning("Web sensor: error polling %s — %s", url, result)
                errored += 1
                continue

            checked += 1
            items: List[Dict[str, str]] = result  # type: ignore[assignment]

            if not items:
                logger.debug("Web sensor: %s — 0 items returned", url)
                continue

            # The first item is the newest. Count items not yet seen.
            new_count = self._count_new(items, state.last_seen_uid)
            if new_count > 0:
                total_new += new_count
                # Update the bookmark to the newest item
                state.last_seen_uid = items[0]["uid"]
                logger.info(
                    "Web sensor: %d new item(s) from %s (newest uid: %s…)",
                    new_count,
                    url,
                    (state.last_seen_uid or "?")[:60],
                )
            else:
                logger.debug("Web sensor: %s — no new items (bookmark current)", url)

        # Persist updated state
        self._save_state()

        result_dict = {
            "new_content": total_new > 0,
            "new_items_count": total_new,
            "feeds_checked": checked,
            "feeds_errored": errored,
            "feeds_skipped": skipped,
            "timestamp": now,
        }
        logger.debug("Web sensor result: %s", result_dict)
        return result_dict

    # ------------------------------------------------------------------
    # Feed fetch + parse
    # ------------------------------------------------------------------

    async def _poll_feed(
        self, url: str, max_items: int, timeout: int
    ) -> List[Dict[str, str]]:
        """Fetch and parse a single feed URL.  Raises on any error."""
        try:
            import aiohttp  # already a Pulse dependency via sensor manager
        except ImportError as exc:
            raise RuntimeError("aiohttp not available — required for WebSensor") from exc

        headers = {
            "User-Agent": "Pulse/WebSensor (github.com/astra-ventures/pulse)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
                ssl=False,  # gracefully handle self-signed certs on local feeds
            ) as resp:
                resp.raise_for_status()
                body = await resp.read()

        return _parse_feed(body, max_items)

    # ------------------------------------------------------------------
    # New-item counting
    # ------------------------------------------------------------------

    @staticmethod
    def _count_new(items: List[Dict[str, str]], last_seen_uid: str) -> int:
        """Count items newer than the last-seen bookmark.

        Items are assumed to be newest-first (standard RSS/Atom convention).
        If ``last_seen_uid`` is empty (first run), we bookmark the newest
        item and report 0 new so we don't dump the whole feed history.
        """
        if not last_seen_uid:
            # First time seeing this feed — just bookmark, don't flood
            return 0

        for idx, item in enumerate(items):
            if item["uid"] == last_seen_uid:
                return idx  # idx items before the bookmark → all new

        # last_seen_uid not found; assume all items are new (feed may have rotated)
        return len(items)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load per-feed positions from the state file."""
        if not self._state_file or not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text())
            for url, saved in data.items():
                if url in self._feed_states:
                    self._feed_states[url].last_seen_uid = saved.get("last_seen_uid", "")
                    self._feed_states[url].last_checked = saved.get("last_checked", 0.0)
            logger.debug("Web sensor: loaded state for %d feed(s)", len(data))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Web sensor: could not load state file: %s", exc)

    def _save_state(self) -> None:
        """Persist per-feed positions to the state file."""
        if not self._state_file:
            return
        payload = {
            url: {
                "last_seen_uid": state.last_seen_uid,
                "last_checked": state.last_checked,
            }
            for url, state in self._feed_states.items()
        }
        try:
            self._state_file.write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            logger.warning("Web sensor: could not save state: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_result() -> dict:
        return {
            "new_content": False,
            "new_items_count": 0,
            "feeds_checked": 0,
            "feeds_errored": 0,
            "feeds_skipped": 0,
            "timestamp": time.time(),
        }
