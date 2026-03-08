import logging
import re
from pathlib import Path
from typing import Optional

import yaml

from pulse.src.instincts.models import Instinct, InstinctOutput, InstinctTrigger

logger = logging.getLogger("pulse.instincts.loader")

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(?P<frontmatter>.*?)\n---\s*\n?(?P<body>.*)$",
    re.DOTALL,
)


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    match = _FRONTMATTER_PATTERN.match(raw)
    if not match:
        raise ValueError("INSTINCT.md is missing YAML frontmatter")
    frontmatter = yaml.safe_load(match.group("frontmatter")) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("INSTINCT frontmatter must be a mapping")
    return frontmatter, match.group("body").strip()


def load_instinct(folder: Path) -> Optional[Instinct]:
    """
    Load an instinct from a folder containing INSTINCT.md.
    Returns None if INSTINCT.md is missing, malformed, or enabled=false.
    """
    instinct_file = folder / "INSTINCT.md"
    if not instinct_file.exists():
        return None

    try:
        frontmatter, body = _split_frontmatter(instinct_file.read_text())
        if not frontmatter.get("enabled", True):
            return None

        triggers = frontmatter.get("triggers") or {}
        output = frontmatter.get("output") or {}

        return Instinct(
            name=str(frontmatter["name"]),
            description=str(frontmatter.get("description", "")),
            version=str(frontmatter.get("version", "1.0")),
            enabled=bool(frontmatter.get("enabled", True)),
            triggers=InstinctTrigger(
                drives=dict(triggers.get("drives") or {}),
                context=dict(triggers.get("context") or {}),
            ),
            cooldown_minutes=int(frontmatter.get("cooldown_minutes", 0)),
            timeout_seconds=int(frontmatter.get("timeout_seconds", 60)),
            output=InstinctOutput(
                log=bool(output.get("log", True)),
                discord=output.get("discord"),
                signal=bool(output.get("signal", False)),
            ),
            script=str(frontmatter["script"]),
            body=body,
            path=folder.resolve(),
        )
    except Exception as e:
        logger.warning(f"Failed to load instinct from {folder}: {e}")
        return None


def load_all_instincts(instincts_dir: Path) -> list[Instinct]:
    """
    Load all instincts from subdirectories of instincts_dir.
    Skips folders without INSTINCT.md. Logs warnings for malformed instincts.
    """
    if not instincts_dir.exists():
        return []

    instincts: list[Instinct] = []
    for folder in sorted(path for path in instincts_dir.iterdir() if path.is_dir()):
        instinct = load_instinct(folder)
        if instinct is not None:
            instincts.append(instinct)
    return instincts
