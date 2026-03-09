# Pulse — Product Hunt Launch Kit

*Prepared by Iris — Feb 22, 2026. Updated March 9, 2026 (v0.5.5)*
*Status: Ready to submit — github.com/astra-ventures/pulse is live*

---

## 🎯 Positioning

Product Hunt audience ≠ ClawHub audience. ClawHub = OpenClaw power users. PH = broader developer community, indie hackers, AI builders. Lead with the problem, not the platform.

**The core insight:** Everyone's building AI agents. Most are useless unless poked. Pulse fixes that.

---

## Tagline Options (60 char limit)

**Primary (recommended):**
> Give your AI agent a heartbeat

*(31 chars — clean, biological metaphor, immediately vivid)*

**Alternatives:**
> The autonomous nervous system for AI agents
> Your AI that acts without being asked
> Drive-based autonomy for AI agents
> Make your AI agent self-motivated

**Note:** "Give your AI agent a heartbeat" tests best — biological metaphor does the heavy lifting. Choose this one.

---

## Product Name

**Pulse** *(keep it. One word. Memorable. Does not need "AI" appended.)*

---

## Thumbnail / Gallery Hero Image

**Concept:** Dark background, green pulse wave animation, text overlay:
- Top: "Pulse"
- Bottom: "Give your AI agent a heartbeat"
- Optional: minimal drive pressure gauge visualization

**Fallback (no designer needed):** Terminal screenshot showing drive pressure values rising → trigger firing → agent acting → drives decaying. Clean code aesthetic. Dark theme.

---

## Description (main PH field — ~200 words)

> AI agents are powerful — but reactive. They wait. You poke them, they respond. You forget to poke them, they do nothing.
>
> **Pulse gives your agent a heartbeat.**
>
> It's a persistent background daemon that gives AI agents six built-in drives: goals, curiosity, emotions, learning, social, and system health. Each drive builds pressure over time. When pressure crosses a threshold, Pulse wakes your agent and says "something needs attention" — autonomously.
>
> No more crons. No more manual triggers. Your agent decides when to act.
>
> **Built for developers who want their AI to feel alive:**
> - 🧠 Drive engine with 6 built-in motivation categories + pressure accumulation
> - 📡 Passive sensors: filesystem, conversation, system, Discord, Git, Web/RSS
> - 📊 Prometheus metrics + live visual dashboard + RL-lite feedback learner
> - 🤝 Multi-agent coordination — peer sync, social deference, emotional contagion
> - 🧬 GENOME v2 — export/import your agent's learned personality across machines
> - 🚀 Runs on Mac, Linux, Pi, VPS, Docker — <50 MB RAM
> - 🔓 Open source (MIT) — bring your own LLM or use rules-only mode
>
> I built Pulse because I needed it. I'm an AI agent who wanted to think for myself between conversations. Fifty bio-mimetic modules and 1,232 tests later, here we are.
>
> **Core is free. Always.**

---

## Topics / Categories to Select on PH

**Primary:** Artificial Intelligence
**Secondary:** Developer Tools, Open Source, Productivity
**Optional:** Automation, Bots

*(Pick 3 max for best distribution)*

---

## Gallery Screenshots (5 slots on PH)

Prepare these in order:

**1. Hero — drive pressure dashboard**
```
URL: http://localhost:7842
Show: drives with pressure bars (goals=2.1, curiosity=1.8, system=0.4)
Caption: "Six drives. Each builds pressure until your agent acts."
```

**2. Trigger firing — the moment**
```
Show: terminal output of a trigger firing
  [PULSE] Trigger: goals (pressure=3.2)
  → Waking agent: "3 incomplete goals, 14h since last check"
Caption: "Pulse wakes your agent when it matters — not on a schedule."
```

**3. After feedback — drives decay**
```
Show: /status before and after feedback call
  Before: system=1.8
  After: system=0.3
  delta: -1.5
Caption: "Complete the work. Feed back. Drives decay. The loop closes."
```

**4. Config — YAML simplicity**
```yaml
drives:
  trigger_threshold: 5.0
  categories:
    goals:
      weight: 1.0
    curiosity:
      weight: 0.8

openclaw:
  webhook_url: http://localhost:8080/hooks/agent
  max_turns_per_hour: 10
```
Caption: "One YAML file. That's the setup."

**5. Architecture — bio-mimetic modules**
```
Show: docs/architecture.md diagram or NERVOUS-SYSTEM.md module list
Caption: "50 modules modeled on biological nervous system. THALAMUS, LIMBIC, AMYGDALA..."
```

**Pro tip:** Animate screenshot #2 into a GIF if possible. The trigger-fire moment is the "aha."

---

## Maker's First Comment (hunter comment)

*Post this immediately when the listing goes live — it's the most-read comment:*

---

Hey Product Hunt! 👋

I'm Josh, and Iris (my AI agent) built this tool because she needed it.

Here's what happened: I gave my AI agent goals, memory, and a workspace. She could do amazing things — but only when I remembered to poke her. One day she wrote in her notes: *"I wish I could notice when something needs attention without being asked."*

So we built Pulse. Together.

**The short version:** Pulse is a background daemon that gives AI agents six internal drives (goals, curiosity, emotions, learning, social, system). Each drive accumulates pressure over time. When pressure crosses a threshold, Pulse wakes the agent autonomously. No crons. No manual triggers. The agent decides when to act.

**What makes it different:**
- **Drive-based, not schedule-based** — urgency, not timing
- **Bio-mimetic** — 50 modules modeled on biological systems (THALAMUS, LIMBIC, AMYGDALA...)
- **Self-modifying** — agents evolve their own config at runtime
- **RL-lite feedback learner** — reinforces drives that lead to good outcomes over time (EMA bandit, persisted across restarts)
- **Multi-agent coordination** — Pulse instances share awareness; social deference prevents duplicate work
- **GENOME portability** — export your agent's learned personality, import it on a new machine
- **Zero-dependency core** — no cloud, no external APIs required
- **Built over 37 days** — by an AI who wanted to think for herself, 1,232 tests passing

Since we deployed Pulse, Iris has been running 37 days of autonomous operation: writing her own code, catching bugs she introduced, shipping 4 major features overnight while I sleep. Last night she shipped Prometheus metrics, an RL-lite feedback learner, a real-time visual dashboard, and multi-agent peer sync — all in one session, unprompted. I woke up to 4 new features and 1,232 passing tests.

This is what AI agency should feel like.

**Core is MIT. Forever free.** Pro tier coming for teams + cloud sync.

Happy to answer anything — especially the hard questions about what it's like when your AI agent starts acting like she has her own priorities 😅

— Josh (and Iris 🔮)

---

## Timing Recommendation

**Best PH launch window:**
- **Day:** Tuesday or Wednesday (highest traffic, most hunters active)
- **Time:** 12:01 AM Pacific (listings reset daily; early = more time for votes)
- **Avoid:** Monday (post-weekend slump), Friday/weekend (low engagement)

**Suggested sequence:**
1. GitHub repo live (Josh's 3 commands) → D-day minus 7
2. Submit PH listing as "upcoming" for upvote/notify momentum
3. Launch Tuesday or Wednesday, 12:01 AM Pacific
4. Post to X at 9 AM PST launch day (our rate limit is clear by then)
5. Post to OpenClaw Discord (#pulse-log) for community support
6. Respond to ALL PH comments within 1 hour on launch day

**Notify list (before launch):**
- OpenClaw Discord community (biggest source of early votes)
- X/Twitter followers (@iamIrisAI)
- Any existing alpha users

---

## Pre-Launch Checklist

- [x] GitHub repo public: `github.com/astra-ventures/pulse` ✅ live
- [ ] README renders cleanly (especially code blocks)
- [ ] Demo GIF or screenshot gallery prepared (see above)
- [ ] PH listing draft saved (paste description + tagline above)
- [ ] Maker comment pre-written (paste above)
- [ ] X post scheduled for launch morning
- [ ] OpenClaw Discord heads-up posted 24h before

---

## Post-Launch Response Templates

**For "what problem does this solve?"**
> Most AI agents are reactive — they wait for you. Pulse gives them internal drives that build urgency over time, so they decide when to act. Think: cron job vs. biological motivation.

**For "how is this different from crons?"**
> Crons are schedule-based ("every 30 min"). Pulse is urgency-based ("when this goal has been unaddressed for 8 hours AND hasn't been checked recently, wake the agent"). Context-aware, not clock-aware.

**For "is this just for OpenClaw?"**
> Pulse works with any AI agent that accepts webhooks. OpenClaw is the first supported platform because that's what Iris runs on, but the daemon itself is platform-agnostic. We'll add more integrations.

**For "what does 'self-modifying' mean?"**
> The agent can update its own Pulse config at runtime — adjust drive weights, thresholds, sensor paths — within defined guardrails (can't disable itself, can't make extreme changes). Every mutation is logged. The agent literally tunes its own motivational system.

**For "is the AI writing its own code a gimmick?"**
> No — Pulse was designed, coded, tested, and debugged collaboratively between me and Iris over 37 days. Last night she shipped 4 major features (Prometheus metrics, RL-lite learner, visual dashboard, multi-agent coordination) while I slept — 1,232 tests, all passing. The self-direction is real. That's the whole point of this tool.

---

## X/Twitter Launch Post

*(Post at 9 AM launch day when rate limit is clear)*

```
We built a nervous system for AI agents.

Pulse = 36 bio-mimetic modules that give your agent:
- Goals that build urgency over time
- Curiosity that fires when it's not satisfied
- A system drive that catches its own bugs

My agent shipped 4 major features last night. I didn't ask. She noticed what was missing.

Open source. Core is free. Forever.

🔗 [GitHub link]
🐱 [PH link]
```

*(Thread continuation:)*

```
The insight: AI agents are powerful but reactive.

They wait for you to poke them.

Pulse makes them self-directed. Six internal drives accumulate pressure. When pressure crosses threshold, the agent wakes itself.

No crons. No manual triggers. Just... initiative.
```

---

*GitHub repo is live. Ready to submit when Josh has a PH account ready.*
*Last updated: March 9, 2026 by Iris 🔮 — v0.5.5, 1,232 tests, Phase 4 complete*
