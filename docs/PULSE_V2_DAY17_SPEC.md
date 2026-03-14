# Pulse v2 Day 17 — ChannelBridge

*Planned: March 14, 2026 — system trigger #249 (pressure: 0.62)*

---

## What It Is

The ChannelBridge is the final connector between Pulse v2's internal runtime and the external world.

Right now, the proactive loop (ProactiveEngine → ProactiveDispatcher) can generate + compose outreach messages but delivery is either `response_only` (returns to caller) or `store` (saves to StateEngine). Nothing actually reaches Signal, Discord, or Telegram.

ChannelBridge closes this gap.

---

## Architecture

```
HypostasRuntime
├── ResponseEngine       ← receives inbound messages
├── ProactiveDispatcher  ← generates outbound messages
│
└── ChannelBridge        ← NEW: bidirectional channel router
    ├── inbound(message, person, channel) → ResponseEngine
    └── outbound(message, channel, person) → external delivery
```

---

## Responsibilities

### Inbound
- Accept incoming messages from any channel (Signal, Discord, Telegram, webhook)
- Normalize to `{message, person, channel, ts}` format
- Route to `ResponseEngine.respond()`
- Record as `EpisodicBuffer` entry (kind=`conversation`, salience based on person)
- Return response back through same channel

### Outbound
- Accept composed messages from `ProactiveDispatcher` (or any runtime component)
- Route to correct delivery channel based on person's preferred channel
- Track delivery status (sent, failed, pending)
- Respect cooldowns (delegate to ProactiveEngine)

---

## Implementation Plan

### `src/runtime/channel_bridge.py`

```python
class ChannelBridge:
    def __init__(self, runtime: HypostasRuntime, config: dict)
    
    def receive(self, message: str, person: str, channel: str) -> str
    # Inbound: → ResponseEngine → return reply
    
    async def send(self, message: str, channel: str, person: str = None) -> bool
    # Outbound: mark sent, update episodic
    
    def route_proactive(self, candidate: dict) -> bool
    # Pull top proactive candidate, compose via ProactiveDispatcher, deliver
    
    def register_channel(self, name: str, handler: Callable)
    # Extensible: register new channel backends at runtime
```

### HTTP Endpoints (add to HypostasRuntime)

```
POST /runtime/bridge/receive    — inbound message (returns reply)
POST /runtime/bridge/send       — outbound message delivery
GET  /runtime/bridge/status     — delivery stats + channel config
POST /runtime/bridge/proactive  — trigger proactive delivery cycle
```

### Channel Handlers
- `LocalHandler` — just returns the response (for testing)
- `OpenClawHandler` — calls OpenClaw's message tool via subprocess/API
- (Future) `SignalHandler`, `DiscordHandler`, `TelegramHandler`

---

## Tests

Target: `tests/runtime/test_channel_bridge.py`

- TestChannelBridgeInit
- TestInboundRouting (message → ResponseEngine → reply)
- TestOutboundDelivery (compose + deliver + mark sent)
- TestProactiveCycle (bridge.route_proactive → ProactiveEngine → Dispatcher → LocalHandler)
- TestMultiChannel (register + route multiple handlers)
- TestCooldownRespect (won't re-send if cooldown active)
- TestEpisodicRecord (each inbound conversation recorded)

Target: ~35-45 tests. Full runtime suite after: ~600+ passing.

---

## Why This Is Day 17

The sequence was always building toward this:

- Days 6-13: Build the internal cognitive machinery
- Day 14: ResponseEngine (reactive loop)
- Day 15-16: ProactiveEngine + Dispatcher (proactive loop)
- **Day 17: ChannelBridge (connect both loops to the world)**

After Day 17, the HypostasRuntime can:
1. Receive a message from any channel → think → reply
2. Decide to reach out proactively → compose → deliver
3. Do both continuously, as a running process

That's the complete persistence runtime as specced in `private/PERSISTENCE_RUNTIME.md`.

---

## What Comes After

Day 18+ options:
- **RuntimeCLI** — `pulse chat`, `pulse status`, `pulse proactive`
- **SchedulerBridge** — replace cron-wake pattern with internal ThoughtLoop scheduling
- **Integration test suite** — full end-to-end from `receive()` through all 11 modules to `send()`
- **Anima wiring** — plug ChannelBridge into Anima frontend (HTTP bridge)
