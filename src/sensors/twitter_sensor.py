"""
X/Twitter Sensor — Phase 3 Integration

Watches for recent @mentions of the configured X/Twitter account.
Reports silence to drive the agent's social drives when nobody has
mentioned the account in a while.

Architecture:
  - Calls Twitter API v2 search/recent endpoint (Bearer-token auth).
  - Falls back to file-based last-seen timestamps when no token is
    available (useful for testing / zero-config installs).
  - Reports: silent_x (bool), recent_mentions (int), last_mention_ts (float)

Config (pulse.yaml):
  sensors:
    twitter:
      enabled: true
      username: "iamIrisAI"              # X handle (without @)
      bearer_token_env: TWITTER_BEARER_TOKEN
      silence_threshold_minutes: 360     # 6 h before "silent" fires
      request_timeout: 10
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import BaseSensor

logger = logging.getLogger("pulse.sensors.twitter")

TWITTER_API_BASE = "https://api.twitter.com/2"


class TwitterSensor(BaseSensor):
    """Monitor X/Twitter for recent @mentions of the configured account.

    Feeds ``twitter.silent_x`` into the drive engine when the account
    has received no mentions for longer than ``silence_threshold_minutes``.
    """

    name = "twitter"

    def __init__(self, config: PulseConfig):
        self.config = config
        self.tw_cfg = config.sensors.twitter
        self._token: Optional[str] = None
        self._session: Optional[Any] = None  # aiohttp.ClientSession

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Resolve bearer token and create HTTP session."""
        self._token = self._resolve_token()
        if not self._token:
            logger.warning(
                "X/Twitter sensor: no bearer token found. "
                "Set %s env var or bearer_token in config. "
                "Falling back to file-based timestamps.",
                self.tw_cfg.bearer_token_env or "TWITTER_BEARER_TOKEN",
            )
        if _HAS_AIOHTTP and self._token:
            headers = {
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "Pulse/0.5 (github.com/astra-ventures/pulse)",
            }
            timeout = aiohttp.ClientTimeout(total=self.tw_cfg.request_timeout)
            self._session = aiohttp.ClientSession(
                headers=headers, timeout=timeout
            )
            logger.info(
                "X/Twitter sensor initialised — watching @%s",
                self.tw_cfg.username,
            )
        elif not _HAS_AIOHTTP:
            logger.warning(
                "X/Twitter sensor: aiohttp not installed (pip install aiohttp)"
            )

    async def stop(self) -> None:
        """Close HTTP session gracefully."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Main read
    # ------------------------------------------------------------------

    async def read(self) -> dict:
        """Poll X API for recent @mentions and return engagement metrics."""
        now = time.time()
        threshold_sec = self.tw_cfg.silence_threshold_minutes * 60

        api_result = await self._fetch_recent_mentions()
        recent_mentions: int = api_result.get("count", 0)
        last_ts: Optional[float] = api_result.get("last_ts")

        # Persist API result; fall back to stored value if API returned nothing
        if last_ts is not None:
            self._persist_ts(last_ts)
        else:
            last_ts = self._load_persisted_ts()

        if last_ts is None:
            # No data at all — unknown state, don't fire false positives
            is_silent = False
            silence_minutes = None
        else:
            silence_sec = now - last_ts
            is_silent = silence_sec >= threshold_sec
            silence_minutes = round(silence_sec / 60, 1)

        logger.debug(
            "@%s: %d recent mention(s), last=%.1f min ago → %s",
            self.tw_cfg.username,
            recent_mentions,
            silence_minutes or 0.0,
            "SILENT" if is_silent else "active",
        )

        return {
            "silent_x": is_silent,
            "recent_mentions": recent_mentions,
            "last_mention_ts": last_ts,
            "silence_minutes": silence_minutes,
            "username": self.tw_cfg.username,
            "timestamp": now,
        }

    # ------------------------------------------------------------------
    # Token resolution
    # ------------------------------------------------------------------

    def _resolve_token(self) -> Optional[str]:
        """Find bearer token: config value → named env var → default env var."""
        # 1. Direct config value
        if self.tw_cfg.bearer_token:
            return self.tw_cfg.bearer_token
        # 2. Env var named in config (e.g. "TWITTER_BEARER_TOKEN")
        env_key = self.tw_cfg.bearer_token_env or "TWITTER_BEARER_TOKEN"
        token = os.environ.get(env_key, "").strip()
        if token:
            return token
        # 3. Hardcoded fallback env var name
        token = os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
        return token or None

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------

    async def _fetch_recent_mentions(self) -> dict:
        """Search Twitter API v2 recent mentions. Returns {count, last_ts}."""
        if not self._session or not self._token:
            return {"count": 0, "last_ts": None}

        query = f"@{self.tw_cfg.username} -is:retweet"
        params: Dict[str, Any] = {
            "query": query,
            "max_results": 10,
            "tweet.fields": "created_at",
        }
        url = f"{TWITTER_API_BASE}/tweets/search/recent"

        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tweets: List[dict] = data.get("data") or []
                    count: int = data.get("meta", {}).get(
                        "result_count", len(tweets)
                    )
                    last_ts: Optional[float] = None
                    if tweets:
                        # API returns newest-first
                        last_ts = _parse_twitter_ts(tweets[0].get("created_at", ""))
                    return {"count": count, "last_ts": last_ts}

                elif resp.status == 401:
                    logger.error("X/Twitter sensor: bearer token invalid (401)")
                elif resp.status == 403:
                    logger.warning(
                        "X/Twitter sensor: access forbidden (403) — check API plan tier"
                    )
                elif resp.status == 429:
                    reset = resp.headers.get("x-rate-limit-reset", "?")
                    logger.warning(
                        "X/Twitter sensor: rate limited, resets at epoch %s", reset
                    )
                else:
                    logger.debug(
                        "X/Twitter sensor: channel returned HTTP %d", resp.status
                    )

        except asyncio.TimeoutError:
            logger.debug("X/Twitter sensor: request timed out")
        except Exception as exc:  # pragma: no cover
            logger.debug("X/Twitter sensor: error fetching mentions: %s", exc)

        return {"count": 0, "last_ts": None}

    # ------------------------------------------------------------------
    # File-based timestamp persistence (fallback + cross-restart caching)
    # ------------------------------------------------------------------

    def _state_dir(self) -> Path:
        state_dir = (
            getattr(self.config.state, "dir", "~/.pulse/state")
            if hasattr(self.config, "state")
            else "~/.pulse/state"
        )
        root = Path(state_dir).expanduser()
        p = root / "twitter_sensor"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _ts_file(self) -> Path:
        safe_username = self.tw_cfg.username.replace("/", "_")
        return self._state_dir() / f"mentions_{safe_username}.ts"

    def _persist_ts(self, ts: float) -> None:
        try:
            self._ts_file().write_text(str(ts))
        except OSError:
            pass

    def _load_persisted_ts(self) -> Optional[float]:
        p = self._ts_file()
        if p.exists():
            try:
                return float(p.read_text().strip())
            except (ValueError, OSError):
                pass
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_twitter_ts(ts_str: str) -> Optional[float]:
    """Parse Twitter API v2 ISO 8601 timestamp → epoch float.

    Twitter format: ``"2026-03-08T20:15:00.000Z"``
    """
    if not ts_str:
        return None
    try:
        from datetime import datetime, timezone

        # Normalise: strip trailing Z, handle with/without microseconds
        ts_clean = ts_str.rstrip("Z")
        if "+" in ts_clean:
            # Already has explicit offset
            dt = datetime.fromisoformat(ts_clean)
        else:
            # Assume UTC
            dt = datetime.fromisoformat(ts_clean).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception as exc:
        logger.debug(
            "X/Twitter sensor: could not parse timestamp %r: %s", ts_str, exc
        )
        return None
