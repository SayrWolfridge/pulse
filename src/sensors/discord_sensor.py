"""
Discord Sensor — Phase 3 Integration

Watches Discord channels for activity. Reports silence to drive the agent's
social/unfinished drives when conversations go quiet for too long.

Architecture:
  - Calls Discord REST API (GET /channels/{id}/messages) with a bot token.
  - Falls back to file-based last-seen timestamps when no token is available
    (useful for testing / zero-config installs).
  - Reports: silent_agents (bool), channel_silences (list), last_message_ts (float)

Config (pulse.yaml):
  sensors:
    discord:
      enabled: true
      bot_token_env: DISCORD_BOT_TOKEN   # env var holding the bot token
      channels:
        - "1473418272551469240"           # pulse-log
      silence_threshold_minutes: 180
      channel_thresholds:
        "1473418272551469240": 240        # override per-channel
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

logger = logging.getLogger("pulse.sensors.discord")

DISCORD_API_BASE = "https://discord.com/api/v10"


class DiscordSensor(BaseSensor):
    """Monitor Discord channels for message activity.

    Feeds `discord.silent_agents` into the drive engine when configured
    channels have been quiet longer than `silence_threshold_minutes`.
    """

    name = "discord"

    def __init__(self, config: PulseConfig):
        self.config = config
        self.dc_cfg = config.sensors.discord
        self._token: Optional[str] = None
        self._last_message_ts: Dict[str, float] = {}  # channel_id → epoch
        self._session: Optional[Any] = None  # aiohttp.ClientSession

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Resolve bot token and create HTTP session."""
        self._token = self._resolve_token()
        if not self._token:
            logger.warning(
                "Discord sensor: no bot token found. "
                "Set %s env var or bot_token in config. "
                "Falling back to file-based timestamps.",
                self.dc_cfg.bot_token_env or "DISCORD_BOT_TOKEN",
            )
        if _HAS_AIOHTTP and self._token:
            headers = {
                "Authorization": f"Bot {self._token}",
                "User-Agent": "Pulse/0.5 (github.com/astra-ventures/pulse)",
            }
            timeout = aiohttp.ClientTimeout(total=self.dc_cfg.request_timeout)
            self._session = aiohttp.ClientSession(
                headers=headers, timeout=timeout
            )
            logger.info(
                "Discord sensor initialised — watching %d channel(s)",
                len(self.dc_cfg.channels),
            )
        else:
            if not _HAS_AIOHTTP:
                logger.warning("Discord sensor: aiohttp not installed (pip install aiohttp)")

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Main read
    # ------------------------------------------------------------------

    async def read(self) -> dict:
        """Poll configured channels and return silence metrics."""
        now = time.time()
        channel_silences: List[dict] = []
        any_silent = False

        for channel_id in self.dc_cfg.channels:
            threshold_min = self.dc_cfg.channel_thresholds.get(
                channel_id, self.dc_cfg.silence_threshold_minutes
            )
            threshold_sec = threshold_min * 60

            # Try API first, then file fallback
            last_ts = await self._last_activity_ts(channel_id)
            if last_ts is None:
                # No data at all — treat as unknown, don't fire false positive
                channel_silences.append(
                    {
                        "channel_id": channel_id,
                        "silent": False,
                        "silence_minutes": None,
                        "status": "unknown",
                    }
                )
                continue

            silence_sec = now - last_ts
            is_silent = silence_sec >= threshold_sec
            if is_silent:
                any_silent = True

            channel_silences.append(
                {
                    "channel_id": channel_id,
                    "silent": is_silent,
                    "silence_minutes": round(silence_sec / 60, 1),
                    "threshold_minutes": threshold_min,
                    "status": "silent" if is_silent else "active",
                }
            )

            logger.debug(
                "Channel %s: %.1f min since last message (threshold: %d min) → %s",
                channel_id,
                silence_sec / 60,
                threshold_min,
                "SILENT" if is_silent else "active",
            )

        return {
            "silent_agents": any_silent,
            "channel_silences": channel_silences,
            "channels_monitored": len(self.dc_cfg.channels),
            "timestamp": now,
        }

    # ------------------------------------------------------------------
    # Token resolution
    # ------------------------------------------------------------------

    def _resolve_token(self) -> Optional[str]:
        """Find bot token: config > env var specified in config > default env var."""
        # 1. Direct config value
        if self.dc_cfg.bot_token:
            return self.dc_cfg.bot_token
        # 2. Env var named in config
        env_key = self.dc_cfg.bot_token_env or "DISCORD_BOT_TOKEN"
        token = os.environ.get(env_key, "").strip()
        if token:
            return token
        # 3. Hardcoded fallback env var name
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        return token or None

    # ------------------------------------------------------------------
    # Activity timestamp retrieval
    # ------------------------------------------------------------------

    async def _last_activity_ts(self, channel_id: str) -> Optional[float]:
        """Return epoch of most recent message in channel, or None if unavailable."""
        # Try Discord REST API
        if self._session and self._token:
            ts = await self._fetch_last_message_ts(channel_id)
            if ts is not None:
                self._last_message_ts[channel_id] = ts
                self._persist_ts(channel_id, ts)
                return ts

        # Fallback: read from persisted state file
        persisted = self._load_persisted_ts(channel_id)
        if persisted is not None:
            return persisted

        return None

    async def _fetch_last_message_ts(self, channel_id: str) -> Optional[float]:
        """Call Discord API to get the timestamp of the most recent message."""
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        params = {"limit": 1}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    messages = await resp.json()
                    if messages:
                        # Discord timestamps are ISO 8601
                        ts_str = messages[0].get("timestamp", "")
                        return _parse_discord_ts(ts_str)
                    else:
                        # Channel exists but has no messages — use epoch 0
                        return 0.0
                elif resp.status == 403:
                    logger.warning(
                        "Discord sensor: bot lacks Read Messages permission for channel %s",
                        channel_id,
                    )
                elif resp.status == 401:
                    logger.error("Discord sensor: bot token is invalid (401)")
                elif resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", 5))
                    logger.warning(
                        "Discord sensor: rate limited, retry after %.1fs", retry_after
                    )
                else:
                    logger.debug(
                        "Discord sensor: channel %s returned HTTP %d",
                        channel_id,
                        resp.status,
                    )
        except asyncio.TimeoutError:
            logger.debug("Discord sensor: timeout fetching channel %s", channel_id)
        except Exception as exc:
            logger.debug("Discord sensor: error fetching channel %s: %s", channel_id, exc)
        return None

    # ------------------------------------------------------------------
    # File-based timestamp persistence (fallback + caching)
    # ------------------------------------------------------------------

    def _state_dir(self) -> Path:
        state_dir = getattr(self.config.state, 'dir', '~/.pulse/state') if hasattr(self.config, 'state') else '~/.pulse/state'
        root = Path(state_dir).expanduser()
        p = root / "discord_sensor"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _ts_file(self, channel_id: str) -> Path:
        return self._state_dir() / f"channel_{channel_id}.ts"

    def _persist_ts(self, channel_id: str, ts: float) -> None:
        try:
            self._ts_file(channel_id).write_text(str(ts))
        except OSError:
            pass

    def _load_persisted_ts(self, channel_id: str) -> Optional[float]:
        p = self._ts_file(channel_id)
        if p.exists():
            try:
                return float(p.read_text().strip())
            except (ValueError, OSError):
                pass
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_discord_ts(ts_str: str) -> Optional[float]:
    """Parse Discord ISO 8601 timestamp → epoch float."""
    if not ts_str:
        return None
    try:
        from datetime import datetime, timezone
        # Discord timestamps: "2026-03-08T20:15:00.000000+00:00"
        # or               "2026-03-08T20:15:00+00:00"
        # Strip microseconds suffix if present for broad compat
        ts_str = ts_str.rstrip("Z")
        if "." in ts_str and "+" in ts_str:
            # "2026-03-08T20:15:00.000000+00:00"
            dt = datetime.fromisoformat(ts_str)
        elif ts_str.endswith("+00:00"):
            dt = datetime.fromisoformat(ts_str)
        else:
            # fallback — assume UTC
            dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception as exc:
        logger.debug("Discord sensor: could not parse timestamp %r: %s", ts_str, exc)
        return None
