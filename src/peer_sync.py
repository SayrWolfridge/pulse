"""
PEER_SYNC — Multi-agent coordination for Pulse.

Polls sibling Pulse instances, reads their drive state + AURA,
and injects social signals into the local THALAMUS so agents
in a constellation can coordinate without duplicating effort.

Design principles:
  - Zero coupling to peer internals — reads only the public /status endpoint
  - Fail-safe: unreachable peers are marked stale, never crash the daemon
  - No shared secrets required: each peer uses its own token
  - Social signal injection keeps salience LOW (0.1–0.25) so peers inform
    rather than override local agency

Typical use: Iris + Scout + Edge each run their own Pulse daemon.
They register each other as peers and naturally de-duplicate work —
if Scout is hammering curiosity tasks, Iris's social_battery reads
that and routes to different drives.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger("pulse.peer_sync")

_DEFAULT_STATE_DIR = Path.home() / ".pulse" / "state"
_PEERS_FILE = _DEFAULT_STATE_DIR / "peers.json"

# Staleness threshold — peer data older than this is treated as unknown
STALE_SECONDS = 120

# Per-peer HTTP timeout (seconds)
REQUEST_TIMEOUT = 5


# ── Data shapes ───────────────────────────────────────────────────────────────


@dataclass
class PeerInfo:
    """Runtime state for one peer instance."""

    name: str
    url: str  # base URL, e.g. "http://192.168.1.50:9720"
    role: str = ""  # optional label, e.g. "scout", "trader", "builder"

    # Polled data (updated by PeerSync.poll)
    reachable: bool = False
    last_seen: float = 0.0
    version: str = ""
    uptime_seconds: float = 0.0

    # Drive summary from peer /status
    drives: Dict[str, float] = field(default_factory=dict)  # name → pressure
    top_drive: str = ""
    top_pressure: float = 0.0

    # AURA state
    mood: str = "unknown"
    energy: float = 1.0
    available: bool = False
    focus: float = 0.5

    # Error tracking
    last_error: str = ""
    consecutive_failures: int = 0


# ── HTTP helper ───────────────────────────────────────────────────────────────


def _fetch_json(url: str, token: str = "", timeout: int = REQUEST_TIMEOUT) -> Optional[dict]:
    """GET url → parsed JSON dict, or None on any error."""
    try:
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError, ValueError) as exc:
        _log.debug("Peer fetch failed %s: %s", url, exc)
        return None


# ── PeerSync ──────────────────────────────────────────────────────────────────


class PeerSync:
    """Manages polling and THALAMUS injection for all configured peers."""

    def __init__(
        self,
        peers_config: List[Dict[str, str]],
        poll_interval: int = 60,
        state_dir: Optional[Path] = None,
    ):
        """
        Args:
            peers_config: list of dicts with keys: name, url, [token], [role]
            poll_interval: seconds between poll cycles
            state_dir: where peers.json is written
        """
        self._poll_interval = poll_interval
        self._state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
        self._peers: Dict[str, PeerInfo] = {}

        # Build PeerInfo registry from config
        for p in peers_config:
            name = p.get("name", "").strip()
            url = p.get("url", "").rstrip("/")
            if not name or not url:
                _log.warning("Peer entry missing name or url — skipping: %s", p)
                continue
            self._peers[name] = PeerInfo(
                name=name,
                url=url,
                role=p.get("role", ""),
            )
        # Store tokens separately (not persisted to disk)
        self._tokens: Dict[str, str] = {
            p.get("name", ""): p.get("token", "") for p in peers_config
        }

        self._last_poll: float = 0.0
        _log.info("PeerSync initialized with %d peer(s): %s", len(self._peers), list(self._peers.keys()))

    # ── Polling ───────────────────────────────────────────────────────────────

    def should_poll(self) -> bool:
        return (time.time() - self._last_poll) >= self._poll_interval

    def poll_all(self) -> Dict[str, PeerInfo]:
        """Synchronous poll of all peers. Updates self._peers and persists state."""
        for name, peer in self._peers.items():
            self._poll_peer(name, peer)
        self._last_poll = time.time()
        self._save()
        return dict(self._peers)

    def _poll_peer(self, name: str, peer: PeerInfo):
        """Poll one peer's /status endpoint and update PeerInfo."""
        token = self._tokens.get(name, "")
        data = _fetch_json(f"{peer.url}/status", token=token)

        if data is None:
            peer.reachable = False
            peer.consecutive_failures += 1
            peer.last_error = f"unreachable at {time.strftime('%H:%M:%S')}"
            _log.debug("Peer %s unreachable (failure #%d)", name, peer.consecutive_failures)
            return

        peer.reachable = True
        peer.last_seen = time.time()
        peer.consecutive_failures = 0
        peer.last_error = ""

        # Version / uptime
        peer.version = data.get("version", "")
        peer.uptime_seconds = data.get("uptime_seconds", 0.0)

        # Drives — /status returns drives as list of {name, pressure, ...} or dict
        drives_raw = data.get("drives", {})
        if isinstance(drives_raw, dict):
            peer.drives = {k: float(v) for k, v in drives_raw.items() if isinstance(v, (int, float))}
        elif isinstance(drives_raw, list):
            peer.drives = {}
            for d in drives_raw:
                if isinstance(d, dict) and "name" in d and "pressure" in d:
                    peer.drives[d["name"]] = float(d["pressure"])

        if peer.drives:
            top = max(peer.drives.items(), key=lambda x: x[1])
            peer.top_drive = top[0]
            peer.top_pressure = top[1]
        else:
            peer.top_drive = ""
            peer.top_pressure = 0.0

        # AURA state (if peer exposes it inside /status or as aura key)
        aura = data.get("aura", {})
        if not aura:
            # Some versions put mood etc at top level
            aura = data
        peer.mood = aura.get("mood", "unknown")
        peer.energy = float(aura.get("energy", 1.0))
        peer.available = bool(aura.get("available", True))
        peer.focus = float(aura.get("focus", 0.5))

        _log.debug(
            "Peer %s: top_drive=%s (%.2f), mood=%s, available=%s",
            name, peer.top_drive, peer.top_pressure, peer.mood, peer.available,
        )

    # ── THALAMUS injection ────────────────────────────────────────────────────

    def inject_thalamus_signals(self):
        """
        Convert current peer state into THALAMUS social signals.

        Signals injected:
          - peer_available   : a peer is up and not overloaded (social presence)
          - peer_busy        : a peer's top drive is the same as ours (avoid duplication)
          - peer_mood_shift  : a peer's mood is notably different (emotional contagion, gentle)
          - peer_offline     : a peer has been unreachable for >2 cycles (system awareness)
        """
        try:
            from pulse.src import thalamus
        except ImportError:
            _log.debug("THALAMUS not available — skipping signal injection")
            return

        now = time.time()
        for name, peer in self._peers.items():
            age = now - peer.last_seen if peer.last_seen > 0 else float("inf")
            stale = age > STALE_SECONDS

            if not peer.reachable or stale:
                if peer.consecutive_failures >= 3:
                    thalamus.append({
                        "source": "peer_sync",
                        "type": "peer_offline",
                        "salience": 0.15,
                        "data": {
                            "peer": name,
                            "role": peer.role,
                            "failures": peer.consecutive_failures,
                        },
                    })
                continue

            # Peer is reachable
            if peer.available and peer.top_pressure < 3.0:
                thalamus.append({
                    "source": "peer_sync",
                    "type": "peer_available",
                    "salience": 0.10,
                    "data": {
                        "peer": name,
                        "role": peer.role,
                        "mood": peer.mood,
                        "energy": peer.energy,
                    },
                })

            # Peer is high-pressure on a drive — social deference
            if peer.top_pressure >= 3.0 and peer.top_drive:
                thalamus.append({
                    "source": "peer_sync",
                    "type": "peer_busy",
                    "salience": 0.20,
                    "data": {
                        "peer": name,
                        "role": peer.role,
                        "drive": peer.top_drive,
                        "pressure": peer.top_pressure,
                    },
                })

            # Mood contagion — very gentle
            if peer.mood not in ("unknown", "neutral"):
                thalamus.append({
                    "source": "peer_sync",
                    "type": "peer_mood_shift",
                    "salience": 0.08,
                    "data": {
                        "peer": name,
                        "mood": peer.mood,
                        "focus": peer.focus,
                    },
                })

    # ── Serialization ─────────────────────────────────────────────────────────

    def _save(self):
        """Persist peer state to disk (without tokens)."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for name, peer in self._peers.items():
            out[name] = {
                "name": peer.name,
                "url": peer.url,
                "role": peer.role,
                "reachable": peer.reachable,
                "last_seen": peer.last_seen,
                "version": peer.version,
                "uptime_seconds": peer.uptime_seconds,
                "drives": peer.drives,
                "top_drive": peer.top_drive,
                "top_pressure": peer.top_pressure,
                "mood": peer.mood,
                "energy": peer.energy,
                "available": peer.available,
                "focus": peer.focus,
                "consecutive_failures": peer.consecutive_failures,
                "last_error": peer.last_error,
            }
        peers_file = self._state_dir / "peers.json"
        tmp = peers_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2))
        tmp.replace(peers_file)

    def get_summary(self) -> Dict[str, Any]:
        """Return a JSON-serializable summary for /state/peers."""
        now = time.time()
        peers_out = []
        reachable_count = 0
        for peer in self._peers.values():
            age = now - peer.last_seen if peer.last_seen > 0 else None
            stale = age is None or age > STALE_SECONDS
            is_live = peer.reachable and not stale
            if is_live:
                reachable_count += 1
            peers_out.append({
                "name": peer.name,
                "role": peer.role,
                "reachable": is_live,
                "last_seen_ago": round(age, 1) if age is not None else None,
                "version": peer.version,
                "top_drive": peer.top_drive,
                "top_pressure": round(peer.top_pressure, 3),
                "mood": peer.mood,
                "energy": round(peer.energy, 2),
                "available": peer.available,
                "consecutive_failures": peer.consecutive_failures,
                "last_error": peer.last_error,
            })
        return {
            "total": len(self._peers),
            "reachable": reachable_count,
            "last_poll": self._last_poll,
            "poll_interval_seconds": self._poll_interval,
            "peers": peers_out,
        }

    def get_peer_names(self) -> List[str]:
        return list(self._peers.keys())


# ── Module-level singleton helpers ────────────────────────────────────────────


_instance: Optional[PeerSync] = None


def init(peers_config: List[Dict[str, str]], poll_interval: int = 60) -> PeerSync:
    """Initialize the global PeerSync singleton."""
    global _instance
    _instance = PeerSync(peers_config=peers_config, poll_interval=poll_interval)
    return _instance


def get_instance() -> Optional[PeerSync]:
    return _instance


def get_status() -> dict:
    """Return peer summary for observation API. Safe if not initialized."""
    if _instance is None:
        return {"enabled": False, "total": 0, "reachable": 0, "peers": []}
    summary = _instance.get_summary()
    summary["enabled"] = True
    return summary
