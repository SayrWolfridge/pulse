"""GERMINAL TASKS — Generative Task Synthesis.

When all drives are high but the work queue is empty (everything blocked on
external deps), CORTEX closes without doing anything. GERMINAL TASKS fixes
this by synthesizing new actionable tasks from what Pulse already has:
current goals, recent memory/logs, HYPOTHALAMUS drives, THALAMUS broadcasts.

Works with zero configuration. Roadmap files (TIERS.md, ROADMAP.md, TODO.md)
are optional enhancements — not dependencies.

Design principle: GENERATE must ship to users who have none of these files.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("pulse.germinal_tasks")

# Deterministic fallback tasks: boring computer work, not pseudo-reflection.
FALLBACK_TASKS_BY_DRIVE = {
    "workspace_git": {
        "title": "Inspect workspace git changes",
        "description": "List dirty workspace files and group them by likely concern. Do not commit or modify anything; produce a concise review target.",
        "rationale": "Workspace git pressure is mechanical state. The useful fallback is inspection, not model-generated ideation.",
        "drive": "workspace_git",
        "effort": "low",
        "requires_external": False,
    },
    "obsidian_git": {
        "title": "Inspect Obsidian git changes",
        "description": "List changed Obsidian notes and identify whether they look like daily notes, tasks, or content edits. Do not commit or modify anything.",
        "rationale": "Obsidian git pressure should turn into a deterministic dirty-file review.",
        "drive": "obsidian_git",
        "effort": "low",
        "requires_external": False,
    },
    "health": {
        "title": "Run health diary preflight",
        "description": "Run the health diary check script and report only concrete missing fields or no-op if the diary is clean.",
        "rationale": "Health pressure is already script-readable; the fallback should use that script instead of asking a model to infer anything.",
        "drive": "health",
        "effort": "low",
        "requires_external": False,
    },
    "emotions": {
        "title": "Run emotions diary preflight",
        "description": "Check emotions cooldown/completion/rotation state and either prepare one clean diary contract or no-op if nothing should be written.",
        "rationale": "Emotions pressure should be handled by the diary preflight contract, not generic reflection.",
        "drive": "emotions",
        "effort": "low",
        "requires_external": False,
    },
    "unfinished": {
        "title": "List open hypotheses",
        "description": "Read the open hypotheses state and list unresolved items with their checks. Do not invent new hypotheses.",
        "rationale": "Unfinished pressure maps to existing open loops; deterministic listing is safer than synthesizing new work.",
        "drive": "unfinished",
        "effort": "low",
        "requires_external": False,
    },
    "sayr_thoughts": {
        "title": "Inspect Sayr thoughts queue",
        "description": "Read the thoughts topics/state and identify whether a cooldown-safe diary note is actually due. If not due, no-op.",
        "rationale": "Sayr-thoughts pressure is a queue/cooldown problem first, not an invitation to freewrite.",
        "drive": "sayr_thoughts",
        "effort": "low",
        "requires_external": False,
    },
}

DEFAULT_NOOP_TASK = {
    "title": "No deterministic GENERATE action",
    "description": "Do not synthesize new work. Keep current state and wait for a concrete sensor, user request, or drive-specific preflight.",
    "rationale": "No drive-specific mechanical fallback matched. Silence is safer than inventing work.",
    "drive": "none",
    "effort": "low",
    "requires_external": False,
}

GENERATE_SYSTEM_PROMPT = """\
You are the task generator for an autonomous AI agent. The agent's work queue \
is empty — all existing tasks are blocked on external dependencies. But the \
agent's drives are still high, meaning it WANTS to work.

Your job: synthesize 1-3 NEW tasks the agent can do RIGHT NOW with NO external \
dependencies. These tasks should be:
- Completable immediately with tools the agent already has
- Relevant to the agent's current goals and drives
- NOT duplicates of existing goals
- NOT requiring human input, API responses, or waiting on anything

You will receive:
- Current goals (what the agent is working toward)
- Recent memory (what the agent has been doing/thinking)
- Drive pressures (what motivates the agent right now)
- Recent broadcasts (what the nervous system is saying)
- Optionally: roadmap/TODO content from project files

Respond with ONLY valid JSON (no markdown, no explanation):
{
  "tasks": [
    {
      "title": "short action-oriented title",
      "description": "what to do and expected outcome (2-3 sentences)",
      "rationale": "why this task matters right now given drives and goals",
      "drive": "which drive this addresses (goals|curiosity|emotions|growth|unfinished)",
      "effort": "low|medium|high",
      "requires_external": false
    }
  ]
}

HARD RULES:
1. Every task MUST have requires_external: false. If it needs human input, API calls, \
or waiting — do NOT include it.
2. Tasks must be SPECIFIC and ACTIONABLE, not vague ("review things", "think about stuff").
3. Maximum 3 tasks. Quality over quantity.
4. Do NOT suggest tasks already in the goals list.
5. Prefer tasks that address the highest-pressure drives.
6. "effort" should reflect actual work: low = <30 min, medium = 30-120 min, high = 2+ hours.
"""


async def generate_tasks(context: dict, config: dict) -> List[dict]:
    """Generate 1-3 actionable tasks from agent context.

    Args:
        context: Dict with keys:
            - goals (list): Current goal descriptions
            - recent_memory (str): ~1000 chars of recent memory/logs
            - drives (dict): name -> pressure mapping
            - thalamus_recent (list): Recent broadcast dicts
        config: Dict with keys from pulse.yaml generative section:
            - enabled (bool)
            - roadmap_files (list[str])
            - max_tasks (int)
            - model (dict): base_url, api_key, model, max_tokens, temperature, timeout_seconds

    Returns:
        List of task dicts, each with: title, description, rationale, drive, effort.
        Only tasks where requires_external=False are returned.
        Returns [DEFAULT_REFLECTION_TASK] on LLM failure.
    """
    if not config.get("enabled", True):
        return []

    max_tasks = config.get("max_tasks", 3)

    use_model = bool(config.get("use_model", False))
    if not use_model:
        tasks = _deterministic_fallback(context, max_tasks)
        logger.info(
            "GENERATE: model disabled, using deterministic fallback (%s)",
            ", ".join(t.get("drive", "?") for t in tasks),
        )
        return tasks

    # Build prompt from context only when a model call is explicitly enabled.
    user_prompt = _build_prompt(context, config)

    # Try LLM call
    model_config = config.get("model", {})
    if not await _model_available(model_config):
        tasks = _deterministic_fallback(context, max_tasks)
        logger.info(
            "GENERATE: configured model unavailable, using deterministic fallback (%s)",
            ", ".join(t.get("drive", "?") for t in tasks),
        )
        return tasks

    try:
        raw_tasks = await _call_llm(user_prompt, model_config)
        tasks = _parse_and_filter(raw_tasks, context.get("goals", []), max_tasks)
        if tasks:
            logger.info(f"GENERATE: synthesized {len(tasks)} tasks")
            return tasks
        # LLM returned nothing usable
        logger.warning("GENERATE: LLM returned no actionable tasks, using fallback")
        return _deterministic_fallback(context, max_tasks)
    except Exception as e:
        logger.warning(f"GENERATE: LLM call failed ({e}), using fallback")
        return _deterministic_fallback(context, max_tasks)


def _deterministic_fallback(context: dict, max_tasks: int) -> List[dict]:
    """Map drive pressure to mechanical tasks without calling a model."""
    drives = context.get("drives") or {}
    ranked = sorted(
        ((name, float(pressure)) for name, pressure in drives.items()),
        key=lambda item: item[1],
        reverse=True,
    )

    tasks = []
    seen = set()
    for drive_name, pressure in ranked:
        if pressure <= 0:
            continue
        task = FALLBACK_TASKS_BY_DRIVE.get(drive_name)
        if not task or task["title"] in seen:
            continue
        tasks.append(dict(task))
        seen.add(task["title"])
        if len(tasks) >= max_tasks:
            break

    if tasks:
        return tasks
    return [dict(DEFAULT_NOOP_TASK)]


def _build_prompt(context: dict, config: dict) -> str:
    """Build the generation prompt from context and optional roadmap files."""
    parts = []

    # Goals
    goals = context.get("goals", [])
    parts.append("## Current Goals")
    if goals:
        for g in goals:
            parts.append(f"- {g}")
    else:
        parts.append("(no goals currently set)")
    parts.append("")

    # Drives
    drives = context.get("drives", {})
    parts.append("## Drive Pressures")
    for name, pressure in sorted(drives.items(), key=lambda x: x[1], reverse=True):
        bar = "#" * int(float(pressure) * 10)
        parts.append(f"- {name}: {float(pressure):.2f} [{bar}]")
    parts.append("")

    # Recent memory
    recent_memory = context.get("recent_memory", "")
    if recent_memory:
        parts.append("## Recent Memory")
        parts.append(recent_memory[:1000])
        parts.append("")

    # Thalamus broadcasts
    thalamus_recent = context.get("thalamus_recent", [])
    if thalamus_recent:
        parts.append("## Recent Nervous System Broadcasts")
        for broadcast in thalamus_recent[-5:]:
            source = broadcast.get("source", "?")
            btype = broadcast.get("type", "?")
            data = broadcast.get("data", {})
            parts.append(f"- [{source}] {btype}: {json.dumps(data, default=str)[:200]}")
        parts.append("")

    # Optional roadmap files
    roadmap_files = config.get("roadmap_files", [])
    workspace_root = config.get("workspace_root", "~/.openclaw/workspace")
    root = Path(workspace_root).expanduser()

    for roadmap_file in roadmap_files:
        filepath = root / roadmap_file
        if filepath.exists():
            try:
                content = filepath.read_text()[:2000]
                parts.append(f"## Roadmap: {roadmap_file}")
                parts.append(content)
                parts.append("")
            except OSError:
                pass

    return "\n".join(parts)


async def _model_available(model_config: dict) -> bool:
    """Return True only when the configured OpenAI-compatible model exists.

    This prevents recurring empty GENERATE calls to missing local models.
    If the endpoint does not support /models, fail closed: deterministic
    fallback is cheaper and safer than hourly 404s/timeouts.
    """
    base_url = model_config.get("base_url", "http://127.0.0.1:11434/v1")
    api_key = model_config.get("api_key", "ollama")
    model = model_config.get("model", "llama3.2:3b")
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
    except Exception:
        return False

    models = data.get("data", []) if isinstance(data, dict) else []
    ids = {item.get("id") for item in models if isinstance(item, dict)}
    return model in ids


async def _call_llm(user_prompt: str, model_config: dict) -> list:
    """Call the LLM and return parsed task list."""
    base_url = model_config.get("base_url", "http://127.0.0.1:11434/v1")
    api_key = model_config.get("api_key", "ollama")
    model = model_config.get("model", "llama3.2:3b")
    max_tokens = model_config.get("max_tokens", 512)
    temperature = model_config.get("temperature", 0.3)
    timeout = model_config.get("timeout_seconds", 10)

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": GENERATE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"LLM API returned {resp.status}: {body[:200]}")
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]

    # Parse JSON from response
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    parsed = json.loads(cleaned)
    return parsed.get("tasks", [])


def _parse_and_filter(
    raw_tasks: list,
    existing_goals: list,
    max_tasks: int,
) -> list:
    """Filter tasks: remove external deps, duplicates, and cap count."""
    required_fields = {"title", "description", "rationale", "drive", "effort"}
    valid_efforts = {"low", "medium", "high"}

    # Normalize existing goals for dedup
    goal_lower = {g.lower().strip() for g in existing_goals if isinstance(g, str)}

    filtered = []
    for task in raw_tasks:
        if not isinstance(task, dict):
            continue

        # Must have all required fields
        if not required_fields.issubset(task.keys()):
            continue

        # Filter out tasks requiring external deps
        if task.get("requires_external", True):
            continue

        # Normalize effort
        if task["effort"] not in valid_efforts:
            task["effort"] = "medium"

        # Dedup: skip if title matches an existing goal
        if task["title"].lower().strip() in goal_lower:
            continue

        # Remove the requires_external field from output (always False at this point)
        clean = {
            "title": task["title"],
            "description": task["description"],
            "rationale": task["rationale"],
            "drive": task["drive"],
            "effort": task["effort"],
        }
        filtered.append(clean)

        if len(filtered) >= max_tasks:
            break

    return filtered
