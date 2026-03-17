"""
Pulse v2 Runtime — HypostasRuntime
====================================
The always-running cognition layer for Iris.

Orchestrates StateEngine + ContextEngine + ThoughtLoop + RuntimeBridge
into one persistent process that keeps Iris alive between messages.

Entry point: python -m pulse.runtime
Health:      GET http://127.0.0.1:9723/runtime/health
Status:      GET http://127.0.0.1:9723/runtime/status
"""

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .state_engine import StateEngine
from .context_engine import ContextEngine
from .thought_loop import ThoughtLoop
from .bridge import RuntimeBridge
from .self_model import SelfModel
from .goal_engine import GoalEngine
from .episodic_buffer import EpisodicBuffer
from .narrative_engine import NarrativeEngine
from .emotion_engine import EmotionEngine
from .relationship_graph import RelationshipGraph
from .context_assembler import ContextAssembler
from .response_engine import ResponseEngine
from .proactive_engine import ProactiveEngine
from .proactive_dispatcher import ProactiveDispatcher
from .channel_bridge import ChannelBridge
from .aura import AuraEngine

# Biological modules (v1 → v2 integration)
from .endocrine import Endocrine
from .limbic import Limbic
from .amygdala import Amygdala
from .circadian import Circadian
from .vagus import Vagus
from .soma import Soma
from .retina import Retina
from .spine import Spine
from .immune import Immune
from .superego import Superego
from .cerebellum import Cerebellum
from .adipose import Adipose
from .engram import Engram as EngramModule
from .nephron import Nephron
from .buffer_mod import Buffer
from .dendrite import Dendrite
from .mirror import Mirror
from .phenotype import Phenotype
from .telomere import Telomere
from .thymus import Thymus
from .vestibular import Vestibular
from .enteric import Enteric
from .callosum import Callosum
from .plasticity import Plasticity

# Biological modules (v1 → v2 consolidation sprint 3)
from .sensors import SensorManager
from .instincts import InstinctExecutor, InstinctRegistry
from .evolution import Evolution
from .drive_engine import DriveEngine

# Biological modules (v1 → v2 consolidation sprint 2)
from .germinal import Germinal
from .hypothalamus import Hypothalamus
from .rem import Rem
from .thalamus import Thalamus
from .genome import Genome
from .chronicle import Chronicle
from .cortex_ext import CortexExt
from .myelin import Myelin
from .oximeter import Oximeter
from .proprioception import Proprioception

logger = logging.getLogger("pulse.runtime")

__all__ = ["HypostasRuntime", "StateEngine", "ContextEngine", "ThoughtLoop", "RuntimeBridge", "SelfModel", "GoalEngine", "EpisodicBuffer", "NarrativeEngine", "EmotionEngine", "RelationshipGraph", "ContextAssembler", "ResponseEngine", "ProactiveEngine", "ProactiveDispatcher", "ChannelBridge", "AuraEngine", "Germinal", "Hypothalamus", "Rem", "Thalamus", "Genome", "Chronicle", "CortexExt", "Myelin", "Oximeter", "Proprioception", "SensorManager", "InstinctExecutor", "InstinctRegistry", "Evolution", "DriveEngine"]


class HypostasRuntime:
    """
    The always-running cognition layer.

    Starts two background threads (StateEngine autosave + ThoughtLoop) and an
    HTTP health endpoint on port 9723.  Optionally attaches a RuntimeBridge to
    an existing PulseDaemon so every triggered session gets live cognitive
    context injected into its prompt.

    Lifecycle::

        runtime = HypostasRuntime()
        runtime.start()
        # ... runs until stopped ...
        runtime.stop()

    Or as a process::

        python -m pulse.runtime
    """

    PORT: int = 9723
    DEFAULT_STATE_DIR: Path = Path("~/.pulse/state").expanduser()

    def __init__(
        self,
        state_dir: Optional[Path] = None,
        daemon=None,
        port: Optional[int] = None,
    ) -> None:
        self._state_dir: Path = state_dir or self.DEFAULT_STATE_DIR
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._port: int = port if port is not None else self.PORT

        # Core components
        self.state: StateEngine = StateEngine(self._state_dir / "hypostas-state.json")
        self.context: ContextEngine = ContextEngine(self._state_dir)
        self.self_model: SelfModel = SelfModel(self.state)
        self.goal_engine: GoalEngine = GoalEngine(self.state)
        self.episodic: EpisodicBuffer = EpisodicBuffer(self.state, path=self._state_dir / "episodes.jsonl")
        self.emotion: EmotionEngine = EmotionEngine(self.state, episodic=self.episodic)
        self.aura: AuraEngine = AuraEngine(
            agent_name=str(self.state.get("meta.agent_name") or "iris")
        )
        # Wire AURA into EmotionEngine for emotional shift broadcasts
        self.emotion._aura = self.aura
        self.relationships: RelationshipGraph = RelationshipGraph(
            context=self.context,
            state=self.state,
            emotion=self.emotion,
        )
        self.narrative: NarrativeEngine = NarrativeEngine(
            state=self.state,
            self_model=self.self_model,
            episodic=self.episodic,
            goal_engine=self.goal_engine,
            context=self.context,
            emotion=self.emotion,
        )
        self.assembler: ContextAssembler = ContextAssembler(
            state=self.state,
            self_model=self.self_model,
            goal_engine=self.goal_engine,
            episodic=self.episodic,
            narrative=self.narrative,
            emotion=self.emotion,
            relationships=self.relationships,
            context=self.context,
            aura=self.aura,
        )
        self.response: ResponseEngine = ResponseEngine(
            assembler=self.assembler,
            narrative=self.narrative,
            emotion=self.emotion,
            episodic=self.episodic,
            self_model=self.self_model,
            state=self.state,
        )
        self.proactive: ProactiveEngine = ProactiveEngine(
            state=self.state,
            emotion=self.emotion,
            goal_engine=self.goal_engine,
            relationships=self.relationships,
            episodic=self.episodic,
        )
        self.dispatcher: ProactiveDispatcher = ProactiveDispatcher(
            proactive=self.proactive,
            response=self.response,
            episodic=self.episodic,
            state=self.state,
        )
        self.channel_bridge: ChannelBridge = ChannelBridge(runtime=self)
        self.thought_loop: ThoughtLoop = ThoughtLoop(
            self.state,
            self.context,
            self_model=self.self_model,
            goal_engine=self.goal_engine,
            episodic=self.episodic,
            narrative=self.narrative,
            runtime=self,
        )
        self.bridge: RuntimeBridge = RuntimeBridge(self)  # passes self so bridge can access .state/.context/.thought_loop

        # --- Biological modules (v1 → v2 integration) ---
        self.endocrine: Endocrine = Endocrine(self.state)
        self.limbic: Limbic = Limbic(self.state)
        self.amygdala: Amygdala = Amygdala(self.state)
        self.circadian: Circadian = Circadian(self.state)
        self.vagus: Vagus = Vagus(self.state)
        self.soma: Soma = Soma(self.state)
        self.retina: Retina = Retina(self.state)
        self.spine: Spine = Spine(self.state)
        self.immune: Immune = Immune(self.state)
        self.superego: Superego = Superego(self.state)
        self.cerebellum: Cerebellum = Cerebellum(self.state)
        self.adipose: Adipose = Adipose(self.state)
        self.engram: EngramModule = EngramModule(self.state, self.episodic)
        self.nephron: Nephron = Nephron(self.state, self.context)
        self.buffer_mod: Buffer = Buffer(self.state)
        self.dendrite: Dendrite = Dendrite(self.state, self.relationships)
        self.mirror: Mirror = Mirror(self.state)
        self.phenotype: Phenotype = Phenotype(self.state)
        self.telomere: Telomere = Telomere(self.state, self.self_model)
        self.thymus: Thymus = Thymus(self.state, self.self_model)
        self.vestibular: Vestibular = Vestibular(self.state)
        self.enteric: Enteric = Enteric(self.state)
        self.callosum: Callosum = Callosum(self.state, self.emotion)
        self.plasticity: Plasticity = Plasticity(self.state, self.goal_engine)

        # --- Biological modules (v1 → v2 consolidation sprint 2) ---
        self.germinal: Germinal = Germinal(self.state, self.goal_engine)
        self.hypothalamus: Hypothalamus = Hypothalamus(self.state, self.goal_engine)
        self.rem: Rem = Rem(self.state, self.context, self.self_model)
        self.thalamus: Thalamus = Thalamus(self.state)
        self.genome: Genome = Genome(self.state, self.self_model)
        self.chronicle: Chronicle = Chronicle(self.state, self.context)
        self.cortex_ext: CortexExt = CortexExt(self.state)
        self.myelin: Myelin = Myelin(self.state, self.context)
        self.oximeter: Oximeter = Oximeter(self.state)
        self.proprioception: Proprioception = Proprioception(self.state, self.self_model)

        # --- Biological modules (v1 → v2 consolidation sprint 3) ---
        self.sensors: SensorManager = SensorManager(self.state, self.context)
        self.instincts: InstinctRegistry = InstinctRegistry()
        self.instinct_executor: InstinctExecutor = InstinctExecutor(self.state, self.instincts)
        self.evolution: Evolution = Evolution(self.state)
        self.drive_engine: DriveEngine = DriveEngine(self.state, self.goal_engine)

        # Optional: existing PulseDaemon (wires EventBus hooks)
        self._daemon = daemon

        # Health server state
        self._health_httpd: Optional[HTTPServer] = None
        self._health_thread: Optional[threading.Thread] = None

        # Runtime state
        self._running: bool = False
        self._start_time: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Boot sequence:
          1. Load state from disk (crash recovery)
          2. Start StateEngine autosave thread
          3. Start ThoughtLoop background thread
          4. Attach bridge to Pulse daemon (if provided)
          5. Start health endpoint
          6. Log SYSTEM_EVENT: runtime_started
        """
        if self._running:
            return

        # 1. Load state from disk (StateEngine loads on __init__ via _load_at_startup;
        #    calling it again here ensures any on-disk changes since init are picked up)
        self.state._load_at_startup()

        # 1b. Load goals (seed or disk)
        try:
            self.goal_engine.load()
        except Exception as e:
            logger.warning("GoalEngine load failed (non-fatal): %s", e)

        # 1c. Load episodic buffer
        try:
            self.episodic.load()
        except Exception as e:
            logger.warning("EpisodicBuffer load failed (non-fatal): %s", e)

        # 1d. Seed endocrine from persisted state if available
        try:
            self.endocrine._load_or_seed()
        except Exception as e:
            logger.warning("Endocrine seed failed (non-fatal): %s", e)

        # 1e. Trigger RelationshipGraph decay sweep to prime in-memory state
        try:
            self.relationships.decay_sweep()
        except Exception as e:
            logger.warning("RelationshipGraph decay sweep failed (non-fatal): %s", e)

        # 2. Start StateEngine autosave thread
        self.state.start_autosave()

        # 3. Start ThoughtLoop background thread
        self.thought_loop.start()

        # 4. Attach RuntimeBridge to daemon (if provided)
        if self._daemon is not None:
            self.bridge.attach(self._daemon)

        # 5. Start health endpoint
        self._start_health_server()

        # 5b. Start cold-tier consolidation background thread (runs hourly)
        self._start_cold_consolidation()

        # 6. Mark running + record start time
        self._running = True
        self._start_time = datetime.now(timezone.utc)

        # Log startup event to hot tier
        try:
            self.context.log_event(
                {
                    "type": "SYSTEM_EVENT",
                    "event": "runtime_started",
                    "ts": self._start_time.isoformat(),
                    "port": self._port,
                    "state_dir": str(self._state_dir),
                }
            )
        except Exception as e:
            logger.warning("Could not log runtime_started event: %s", e)

        logger.info(
            "HypostasRuntime started | health: http://127.0.0.1:%d/runtime/health",
            self._port,
        )

    def stop(self) -> None:
        """
        Graceful shutdown:
          1. Stop ThoughtLoop (finish current cycle)
          2. Log SYSTEM_EVENT: runtime_stopped
          3. Final StateEngine serialize
          4. Stop health server
        """
        if not self._running:
            return

        # 1. Stop ThoughtLoop gracefully
        try:
            self.thought_loop.stop()
        except Exception as e:
            logger.warning("ThoughtLoop stop error: %s", e)

        # 2. Log runtime_stopped before final save so the event is persisted
        try:
            self.context.log_event(
                {
                    "type": "SYSTEM_EVENT",
                    "event": "runtime_stopped",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "uptime_seconds": self._uptime_seconds(),
                }
            )
        except Exception as e:
            logger.warning("Could not log runtime_stopped event: %s", e)

        # 3. Final state serialize (accumulate uptime before saving)
        try:
            current = self.state.get("meta.total_uptime_seconds") or 0
            self.state.set(
                "meta.total_uptime_seconds",
                current + self._uptime_seconds(),
            )
            self.state.save()
        except Exception as e:
            logger.warning("Final state save error: %s", e)

        # 4. Stop health server
        if self._health_httpd is not None:
            try:
                self._health_httpd.shutdown()
            except Exception as e:
                logger.warning("Health server shutdown error: %s", e)

        self._running = False
        logger.info("HypostasRuntime stopped")

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a plain-dict status snapshot (JSON-serialisable)."""
        return {
            "running": self._running,
            "uptime_seconds": self._uptime_seconds(),
            "port": self._port,
            "state_dir": str(self._state_dir),
            "thought_loop": self.thought_loop.status(),
            "bridge": self.bridge.status(),
            "self_model": self.self_model.status(),
            "goals": self.goal_engine.status(),
            "episodic": self.episodic.status(),
            "narrative": self.narrative.snapshot(),
            "emotion": self.emotion.status(),
            "relationships": self.relationships.status() if hasattr(self.relationships, "status") else {"count": len(self.context.get_all_relationships() or {})},
            "assembler": self.assembler.snapshot(),
            "response": self.response.status(),
            "proactive": self.proactive.snapshot(),
            "dispatcher": self.dispatcher.status(),
            "channel_bridge": self.channel_bridge.status(),
            "aura": self.aura.snapshot(),
            # Biological modules
            "endocrine": self.endocrine.status(),
            "limbic": self.limbic.status(),
            "amygdala": self.amygdala.status(),
            "circadian": self.circadian.status(),
            "vagus": self.vagus.status(),
            "soma": self.soma.status(),
            "spine": self.spine.status(),
            "retina": self.retina.status(),
            "immune": self.immune.status(),
            "superego": self.superego.status(),
            "cerebellum": self.cerebellum.status(),
            "adipose": self.adipose.status(),
            "engram": self.engram.status(),
            "nephron": self.nephron.status(),
            "buffer": self.buffer_mod.status(),
            "dendrite": self.dendrite.status(),
            "mirror": self.mirror.status(),
            "phenotype": self.phenotype.status(),
            "telomere": self.telomere.status(),
            "thymus": self.thymus.status(),
            "vestibular": self.vestibular.status(),
            "enteric": self.enteric.status(),
            "callosum": self.callosum.status(),
            "plasticity": self.plasticity.status(),
            # Sprint 2 biological modules
            "germinal": self.germinal.status(),
            "hypothalamus": self.hypothalamus.status(),
            "rem": self.rem.status(),
            "thalamus": self.thalamus.status(),
            "genome": self.genome.status(),
            "chronicle": self.chronicle.status(),
            "cortex_ext": self.cortex_ext.status(),
            "myelin": self.myelin.status(),
            "oximeter": self.oximeter.status(),
            "proprioception": self.proprioception.status(),
            # Sprint 3 biological modules
            "sensors": self.sensors.status(),
            "instincts": self.instincts.status(),
            "instinct_executor": self.instinct_executor.status(),
            "evolution": self.evolution.status(),
            "drive_engine": self.drive_engine.status(),
        }

    # ------------------------------------------------------------------
    # Ingest (observe-only message processing)
    # ------------------------------------------------------------------

    # Salience thresholds for episode gating
    _BIOSENSOR_PREFIXES = ("[biosensor]", "[BIOSENSOR]", "biosensor")
    _SYSTEM_PREFIXES = ("[system]", "[SYSTEM]", "session nearing compaction")
    _ROUTINE_MAX_CHARS = 30  # Very short messages are probably routine

    def _start_cold_consolidation(self) -> None:
        """Background thread: move aged hot-tier entries to cold every hour."""
        import threading as _t
        runtime = self

        def _run():
            import time as _time
            HOT_TIER_MAX_AGE_H = 48
            while True:
                try:
                    _time.sleep(3600)
                    cutoff = _time.time() - HOT_TIER_MAX_AGE_H * 3600
                    all_hot = runtime.context.hot.get_all()
                    aged = [e for e in all_hot if float(e.get("ts_unix", 0) or 0) < cutoff]
                    if aged:
                        n = runtime.context.cold.encode(aged)
                        logger.info("Cold consolidation: archived %d aged hot-tier entries", n)
                except Exception as e:
                    logger.debug("Cold consolidation error: %s", e)

        _t.Thread(target=_run, daemon=True, name="cold-consolidation").start()

    @staticmethod
    def _extract_themes(message: str) -> list[str]:
        """
        Extract 1-3 topic themes from a message without LLM.
        Uses keyword matching against known domain vocabulary.
        """
        text = message.lower()
        theme_map = {
            "weather bot": ["weather", "polymarket", "bet", "forecast", "kalshi"],
            "SOMA / biosensor": ["biosensor", "soma", "sleep", "hrv", "heart rate", "biometric"],
            "constellation": ["constellation", "vera", "mira", "sage", "lyra", "discord", "agent"],
            "Hypostas / Anima": ["anima", "gnosis", "hypostas", "companion", "stripe"],
            "building / coding": ["commit", "deploy", "build", "fix", "ship", "code", "error", "bug"],
            "trading / SDCA": ["sdca", "btc", "eth", "z-score", "dca", "crypto"],
            "Pulse runtime": ["pulse", "runtime", "context", "narrative", "episodic", "cold tier"],
            "voice / embodiment": ["voice", "twilio", "ngrok", "cloudflared", "tunnel", "call"],
            "convergence": ["convergence", "embodiment", "body", "merge", "upload"],
        }
        found = []
        for theme, keywords in theme_map.items():
            if any(kw in text for kw in keywords):
                found.append(theme)
            if len(found) >= 3:
                break
        return found

    @staticmethod
    def _message_salience(message: str, person: str, direction: str) -> float:
        """Compute salience for an ingest message. Returns 0.0 to skip recording."""
        msg_lower = message.lower().strip()

        # Biosensor / system heartbeat messages — not episodically significant
        biosensor_markers = ("[biosensor]", "biosensor", "hr zone=", "hrv stress=")
        if any(msg_lower.startswith(m) or m in msg_lower[:60] for m in biosensor_markers):
            return 0.0

        # System/session compaction messages
        system_markers = ("session nearing compaction", "[system]", "system:")
        if any(msg_lower.startswith(m) for m in system_markers):
            return 0.0

        # Very short outbound messages (e.g., "okay", "yes", "done")
        if direction == "sent" and len(message.strip()) < 20:
            return 1.5

        # Josh messages: scale by length / apparent substance
        if person.lower() == "josh" and direction == "received":
            if len(message) < 15:
                return 3.0   # Very short Josh message (acknowledge, not significant)
            if len(message) < 60:
                return 4.0   # Short Josh message
            return 5.5       # Substantive Josh message

        # Generic received
        if direction == "received":
            return 3.5

        # Sent messages (medium length)
        return 3.0

    def ingest_message(
        self,
        message: str,
        person: str = "josh",
        channel: str = "signal",
        direction: str = "received",
    ) -> dict:
        """
        Observe-only message processing. No response generated.

        Pipeline:
          1. Log to hot tier (MESSAGE_RECEIVED or MESSAGE_SENT)
          2. Update relationship graph
          3. Trigger emotional processing
          4. Broadcast to Thalamus bus
          5. Record episodic trace (gated by salience)
          6. Return acknowledgment
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Log to hot tier
        event_type = "MESSAGE_RECEIVED" if direction == "received" else "MESSAGE_SENT"
        self.context.log_event({
            "type": event_type,
            "content": message,
            "source": person,
            "channel": channel,
        })

        # 2. Update relationship graph (always — keeps bond current)
        try:
            themes = self._extract_themes(message) if len(message) > 20 else []
            self.relationships.record_event(
                person=person,
                kind="message",
                note=f"[{channel}] {message[:200]}",
                themes=themes or None,
            )
        except Exception as e:
            logger.warning("Ingest: relationship update failed: %s", e)

        # 3. Emotional processing
        try:
            msg_lower = message.lower().strip()
            is_biosensor = any(m in msg_lower[:60] for m in ("biosensor", "hr zone=", "hrv stress="))
            if not is_biosensor:
                if direction == "received" and person.lower() in ("josh",):
                    self.emotion.apply_event("JOSH_MESSAGE", note=message[:200])
                elif direction == "received":
                    self.emotion.update("joy", 0.05, reason=f"message from {person}")
        except Exception as e:
            logger.warning("Ingest: emotion update failed: %s", e)

        # 4. Thalamus broadcast
        try:
            salience = self._message_salience(message, person, direction)
            self.thalamus.append({
                "source": f"ingest:{channel}",
                "type": event_type,
                "salience": salience,
                "data": {
                    "person": person,
                    "direction": direction,
                    "channel": channel,
                    "preview": message[:120],
                },
            })
        except Exception as e:
            logger.warning("Ingest: thalamus append failed: %s", e)

        # 5. Episodic trace — gated by salience (skip routine/biosensor messages)
        try:
            salience = self._message_salience(message, person, direction)
            if salience >= 3.5:
                self.episodic.record(
                    kind="conversation",
                    title=f"{'Received' if direction == 'received' else 'Sent'} via {channel} — {person}",
                    content=message[:500],
                    salience=salience,
                    tags=["ingest", f"channel:{channel}", f"person:{person}"],
                    source="system",
                )
        except Exception as e:
            logger.warning("Ingest: episodic record failed: %s", e)

        # 6. Invalidate assembler cache so next /context call reflects this message
        try:
            self.assembler.invalidate(person=person)
        except Exception as e:
            logger.debug("Ingest: assembler invalidate failed: %s", e)

        return {
            "ok": True,
            "event_type": event_type,
            "person": person,
            "channel": channel,
            "ts": now_iso,
        }

    # ------------------------------------------------------------------
    # Health server (port 9723)
    # ------------------------------------------------------------------

    def _start_health_server(self) -> None:
        """Start a simple HTTP health endpoint on port 9723."""
        runtime = self  # capture for handler closure

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/runtime/health":
                    body = json.dumps(
                        {
                            "status": "ok",
                            "running": runtime._running,
                            "uptime_seconds": runtime._uptime_seconds(),
                        }
                    ).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/status":
                    body = json.dumps(runtime.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/goals":
                    body = json.dumps(runtime.goal_engine.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/episodes":
                    body = json.dumps(runtime.episodic.snapshot(top=20)).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/episodes/context":
                    body = json.dumps({"narrative": runtime.episodic.context_narrative()}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/narrative":
                    body = json.dumps(runtime.narrative.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/emotion":
                    body = json.dumps(runtime.emotion.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/emotion/events":
                    body = json.dumps({"events": runtime.emotion.known_events()}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/relationships":
                    body = json.dumps(runtime.relationships.snapshot(top=20)).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/relationships/reconnect":
                    body = json.dumps({"candidates": runtime.relationships.reconnect_candidates()}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/proactive":
                    body = json.dumps(runtime.proactive.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/proactive/top":
                    top = runtime.proactive.top_candidate()
                    body = json.dumps({"top": top.to_dict() if top else None}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/bridge/status":
                    body = json.dumps(runtime.channel_bridge.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/aura":
                    body = json.dumps(runtime.aura.snapshot()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/aura/poll":
                    events = runtime.aura.poll()
                    body = json.dumps({"events": events, "count": len(events)}).encode()
                    self._respond(200, body)
                elif self.path.startswith("/runtime/aura/agent/"):
                    agent_name = self.path.split("/")[-1]
                    state = runtime.aura.get_agent_state(agent_name)
                    body = json.dumps({"agent": agent_name, "state": state}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/continuity":
                    data = runtime.narrative.get_continuity_data()
                    body = json.dumps(data).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/creative":
                    creative_dir = Path("~/.pulse/creative").expanduser()
                    files = []
                    if creative_dir.exists():
                        all_files = sorted(creative_dir.glob("*.md"), reverse=True)
                        for f in all_files[:10]:
                            try:
                                content = f.read_text()[:2000]
                                files.append({
                                    "filename": f.name,
                                    "path": str(f),
                                    "size": f.stat().st_size,
                                    "modified": datetime.fromtimestamp(
                                        f.stat().st_mtime, tz=timezone.utc
                                    ).isoformat(),
                                    "content": content,
                                })
                            except Exception:
                                files.append({"filename": f.name, "error": "read failed"})
                    body = json.dumps({
                        "count": len(files),
                        "files": files,
                    }).encode()
                    self._respond(200, body)
                elif self.path.startswith("/runtime/cold/search"):
                    parsed = urlparse(self.path)
                    qs = parse_qs(parsed.query)
                    query = qs.get("q", [""])[0]
                    top_k = int(qs.get("k", ["5"])[0])
                    if not query:
                        self._respond(400, json.dumps({"error": "missing q param"}).encode())
                    else:
                        results = runtime.context.cold.search(query, top_k=top_k)
                        self._respond(200, json.dumps({
                            "query": query,
                            "results": results,
                            "total_indexed": runtime.context.cold.count(),
                        }).encode())
                elif self.path.startswith("/runtime/context"):
                    try:
                        parsed = urlparse(self.path)
                        qs = parse_qs(parsed.query)
                        fmt = qs.get("format", ["standard"])[0]
                        person = qs.get("person", [None])[0]
                        text = runtime.assembler.assemble(fmt=fmt, person=person)
                        body = json.dumps({
                            "context": text,
                            "format": fmt,
                            "person": person,
                            "chars": len(text),
                            "snapshot": runtime.assembler.snapshot(),
                        }).encode()
                        self._respond(200, body)
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                # --- Biological module endpoints ---
                elif self.path == "/runtime/endocrine":
                    body = json.dumps(runtime.endocrine.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/limbic":
                    body = json.dumps(runtime.limbic.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/circadian":
                    body = json.dumps(runtime.circadian.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/soma":
                    body = json.dumps(runtime.soma.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/spine/health":
                    body = json.dumps(runtime.spine.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/amygdala":
                    body = json.dumps(runtime.amygdala.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/vagus":
                    body = json.dumps(runtime.vagus.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/enteric":
                    body = json.dumps(runtime.enteric.status()).encode()
                    self._respond(200, body)
                # --- Sprint 2 biological module endpoints ---
                elif self.path == "/runtime/germinal":
                    body = json.dumps(runtime.germinal.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/hypothalamus":
                    body = json.dumps(runtime.hypothalamus.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/thalamus":
                    body = json.dumps(runtime.thalamus.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/chronicle":
                    body = json.dumps(runtime.chronicle.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/genome":
                    body = json.dumps(runtime.genome.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/genome/export":
                    body = json.dumps(runtime.genome.export_genome()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/rem":
                    body = json.dumps(runtime.rem.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/cortex_ext":
                    body = json.dumps(runtime.cortex_ext.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/myelin":
                    body = json.dumps(runtime.myelin.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/oximeter":
                    body = json.dumps(runtime.oximeter.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/proprioception":
                    body = json.dumps(runtime.proprioception.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/thalamus/recent":
                    body = json.dumps({"entries": runtime.thalamus.read_recent(20)}).encode()
                    self._respond(200, body)
                # --- Sprint 3 endpoints ---
                elif self.path == "/runtime/sensors":
                    body = json.dumps(runtime.sensors.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/instincts":
                    body = json.dumps(runtime.instincts.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/evolution":
                    body = json.dumps(runtime.evolution.status()).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/drives":
                    body = json.dumps(runtime.drive_engine.status()).encode()
                    self._respond(200, body)
                elif self.path.startswith("/runtime/cold-tier/search"):
                    # Quick keyword search over cold-tier archives
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    query = (params.get("q") or params.get("query") or [""])[0]
                    top_k = int((params.get("top_k") or ["10"])[0])
                    results = runtime.context.cold.search(query, top_k=top_k)
                    body = json.dumps({"query": query, "results": results}).encode()
                    self._respond(200, body)
                elif self.path == "/runtime/cold-tier/status":
                    body = json.dumps({
                        "total": runtime.context.cold.count(),
                        "archives": [
                            a.name for a in sorted(
                                runtime.context.cold.index_dir.glob("archive-*.jsonl"), reverse=True
                            )[:20]
                        ],
                    }).encode()
                    self._respond(200, body)
                else:
                    self._respond(404, b"Not found")

            def _respond(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/runtime/episodes":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        ep = runtime.episodic.record(
                            kind=payload.get("kind", "other"),
                            title=payload.get("title", ""),
                            content=payload.get("content", ""),
                            salience=payload.get("salience"),
                            tags=payload.get("tags"),
                            source=payload.get("source", "manual"),
                            linked_goal=payload.get("linked_goal"),
                        )
                        self._respond(201, json.dumps(ep).encode())
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/narrative/refresh":
                    try:
                        runtime.narrative.invalidate()
                        runtime.narrative._last_source_hash = ""  # force actual rebuild
                        text = runtime.narrative.build()
                        body = json.dumps({"text": text, "chars": len(text)}).encode()
                        self._respond(200, body)
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/goals/update":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        goal_id = payload.get("id", "")
                        field = payload.get("field", "")
                        value = payload.get("value")

                        if field == "status" and value == "completed":
                            ok = runtime.goal_engine.complete_goal(goal_id)
                        elif field == "progress":
                            ok = runtime.goal_engine.update_progress(goal_id, float(value))
                        elif field == "add_blocker":
                            ok = runtime.goal_engine.add_blocker(goal_id, str(value))
                        elif field == "remove_blocker":
                            ok = runtime.goal_engine.remove_blocker(goal_id, str(value))
                        else:
                            self._respond(400, json.dumps({"error": f"unknown field: {field}"}).encode())
                            return

                        result = {"ok": ok, "goals": runtime.goal_engine.status()}
                        self._respond(200, json.dumps(result).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/emotion/event":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        event_name = payload.get("event", "")
                        note = payload.get("note", "")
                        applied = runtime.emotion.apply_event(event_name, note=note)
                        body = json.dumps({
                            "ok": True,
                            "event": event_name,
                            "applied": applied,
                            "emotion": runtime.emotion.status(),
                        }).encode()
                        self._respond(200, body)
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/emotion/update":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        emotion_name = payload.get("emotion", "")
                        delta = float(payload.get("delta", 0))
                        reason = payload.get("reason", "")
                        new_val = runtime.emotion.update(emotion_name, delta, reason=reason)
                        body = json.dumps({
                            "ok": True,
                            "emotion": emotion_name,
                            "new_value": new_val,
                            "state": runtime.emotion.status(),
                        }).encode()
                        self._respond(200, body)
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/context/prime":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        fmt = payload.get("format", "standard")
                        person = payload.get("person")
                        # Invalidate cache for fresh assembly
                        runtime.assembler.invalidate(fmt=fmt, person=person)
                        text = runtime.assembler.assemble(fmt=fmt, person=person)
                        body = json.dumps({
                            "context": text,
                            "format": fmt,
                            "person": person,
                            "chars": len(text),
                        }).encode()
                        self._respond(200, body)
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/respond":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        message = str(payload.get("message", "")).strip()
                        if not message:
                            self._respond(400, json.dumps({"error": "missing message"}).encode())
                            return
                        person = payload.get("person")
                        fmt = payload.get("format", "compact")
                        max_tokens = int(payload.get("max_tokens", 400))
                        result = runtime.response.respond(
                            message,
                            person=person,
                            fmt=fmt,
                            max_tokens=max_tokens,
                        )
                        self._respond(200, json.dumps(result.to_dict()).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/bridge/receive":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        message = str(payload.get("message", "")).strip()
                        if not message:
                            self._respond(400, json.dumps({"error": "missing message"}).encode())
                            return
                        person = str(payload.get("person", "josh"))
                        channel = str(payload.get("channel", "local"))
                        fmt = str(payload.get("format", "compact"))
                        max_tokens = int(payload.get("max_tokens", 400))
                        deliver = bool(payload.get("deliver", False))

                        if deliver:
                            result = runtime.channel_bridge.receive_and_send(
                                message,
                                person=person,
                                channel=channel,
                                fmt=fmt,
                                max_tokens=max_tokens,
                            )
                            self._respond(200, json.dumps(result).encode())
                        else:
                            text = runtime.channel_bridge.receive(
                                message,
                                person=person,
                                channel=channel,
                                fmt=fmt,
                                max_tokens=max_tokens,
                            )
                            self._respond(200, json.dumps({"text": text, "person": person, "channel": channel}).encode())
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/bridge/send":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        message = str(payload.get("message", "")).strip()
                        if not message:
                            self._respond(400, json.dumps({"error": "missing message"}).encode())
                            return
                        channel = str(payload.get("channel", "local"))
                        person = payload.get("person")
                        ok = runtime.channel_bridge.send(message, channel=channel, person=person)
                        self._respond(200, json.dumps({"ok": bool(ok), "channel": channel, "person": person}).encode())
                    except (ValueError, KeyError) as exc:
                        self._respond(400, json.dumps({"error": str(exc)}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/bridge/proactive":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        person = str(payload.get("person", "josh"))
                        channel = payload.get("channel")
                        max_tokens = int(payload.get("max_tokens", 250))
                        result = runtime.channel_bridge.deliver_proactive(
                            person=person,
                            channel=str(channel) if channel else None,
                            max_tokens=max_tokens,
                        )
                        status_code = 200 if result.get("dispatched") else 204
                        self._respond(status_code, json.dumps(result).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/proactive/sent":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        kind = str(payload.get("kind", "")).strip()
                        if not kind:
                            self._respond(400, json.dumps({"error": "missing kind"}).encode())
                            return
                        runtime.proactive.mark_sent(kind)
                        # Optional: mark milestone goal ids as announced
                        if kind == "milestone":
                            goal_ids = payload.get("goal_ids") or []
                            if isinstance(goal_ids, list) and goal_ids:
                                existing = set(runtime.state.get("proactive.announced_goal_ids") or [])
                                for gid in goal_ids:
                                    if gid:
                                        existing.add(str(gid))
                                runtime.state.set("proactive.announced_goal_ids", sorted(existing))
                        self._respond(200, json.dumps({"ok": True, "kind": kind}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/proactive/deliver":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        mode = str(payload.get("mode", "response_only")).strip()
                        person = str(payload.get("person", "josh")).strip()
                        max_tokens = int(payload.get("max_tokens", 250))
                        result = runtime.dispatcher.dispatch(
                            mode=mode,
                            person=person,
                            max_tokens=max_tokens,
                        )
                        status_code = 200 if result.dispatched else 204
                        self._respond(status_code, json.dumps(result.to_dict()).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/aura/broadcast":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        kind = str(payload.get("kind", "insight"))
                        data = payload.get("payload", {})
                        ttl = float(payload.get("ttl_hours", 24.0))
                        event = runtime.aura.broadcast(kind, data, ttl)
                        self._respond(200, json.dumps(event).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/soma/update":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        # Apply energy delta
                        delta = float(payload.get("energy_delta", 0.0))
                        if delta > 0:
                            runtime.soma.replenish(delta)
                        elif delta < 0:
                            runtime.soma.spend_energy(int(abs(delta) * 1000))
                        # Apply posture
                        posture = payload.get("posture")
                        if posture:
                            runtime.soma._state.set("soma.posture", posture)
                        # Store full biosensor snapshot in soma state
                        runtime.soma._state.set("soma.last_biosensor", payload)
                        # If deep sleep, update CIRCADIAN context
                        if payload.get("sleep_stage") == "deep":
                            runtime.soma._state.set("soma.recovery_mode", True)
                        else:
                            runtime.soma._state.set("soma.recovery_mode", False)
                        self._respond(200, json.dumps({"ok": True, "soma": runtime.soma.status()}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/ingest":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw) if raw else {}
                        message = str(payload.get("message", "")).strip()
                        if not message:
                            self._respond(400, json.dumps({"error": "missing message"}).encode())
                            return
                        person = str(payload.get("person", payload.get("sender", "josh")))
                        channel = str(payload.get("channel", "signal"))
                        direction = str(payload.get("direction", "received"))
                        result = runtime.ingest_message(
                            message=message,
                            person=person,
                            channel=channel,
                            direction=direction,
                        )
                        self._respond(200, json.dumps(result).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                elif self.path == "/runtime/relationships/event":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length)
                        payload = json.loads(raw)
                        rec = runtime.relationships.record_event(
                            person=str(payload.get("person", "")),
                            kind=str(payload.get("kind", "message")),
                            note=str(payload.get("note", "")),
                            themes=payload.get("themes"),
                            delta_bond=payload.get("delta_bond"),
                            tier=payload.get("tier"),
                        )
                        self._respond(200, json.dumps({"ok": True, "relationship": rec}).encode())
                    except Exception as exc:
                        self._respond(500, json.dumps({"error": str(exc)}).encode())
                else:
                    self._respond(404, b"Not found")

            def log_message(self, fmt: str, *args) -> None:  # noqa: ANN001
                pass  # Suppress access logs — keep logs clean

        try:
            self._health_httpd = HTTPServer(("127.0.0.1", self._port), _Handler)
        except OSError as e:
            logger.warning(
                "Could not bind health server on port %d: %s (continuing without health endpoint)",
                self._port,
                e,
            )
            return

        self._health_thread = threading.Thread(
            target=self._health_httpd.serve_forever,
            daemon=True,
            name="hypostas-health",
        )
        self._health_thread.start()
        logger.info("Health endpoint: http://127.0.0.1:%d/runtime/health", self._port)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return (datetime.now(timezone.utc) - self._start_time).total_seconds()
