import operator
from pathlib import Path

from pulse.src.instincts.loader import load_all_instincts
from pulse.src.instincts.models import Instinct


class InstinctRegistry:
    def __init__(self, instincts_dir: Path):
        self.instincts_dir = instincts_dir
        self._instincts: list[Instinct] = load_all_instincts(instincts_dir)

    def match(self, drive_state: dict[str, float], context: dict) -> list[Instinct]:
        """
        Return instincts whose trigger conditions are satisfied.
        """
        matched: list[tuple[float, Instinct]] = []
        for instinct in self._instincts:
            if not all(
                self._evaluate_condition(condition, drive_state.get(drive_name, 0.0))
                for drive_name, condition in instinct.triggers.drives.items()
            ):
                continue

            if not all(
                context.get(key) == value
                for key, value in instinct.triggers.context.items()
            ):
                continue

            pressure_sum = sum(
                drive_state.get(drive_name, 0.0)
                for drive_name in instinct.triggers.drives
            )
            matched.append((pressure_sum, instinct))

        matched.sort(key=lambda item: item[0], reverse=True)
        return [instinct for _, instinct in matched]

    def _evaluate_condition(self, condition: str, actual: float) -> bool:
        """Parse and evaluate '>= 3.0' style condition strings."""
        condition = condition.strip()
        operators = {
            ">=": operator.ge,
            "<=": operator.le,
            "!=": operator.ne,
            ">": operator.gt,
            "<": operator.lt,
            "==": operator.eq,
        }
        for op in (">=", "<=", "!=", ">", "<", "=="):
            if condition.startswith(op):
                threshold = float(condition[len(op) :].strip())
                return operators[op](actual, threshold)
        return False

    def all_instincts(self) -> list[Instinct]:
        return list(self._instincts)

    def reload(self):
        """Reload all instincts from disk."""
        self._instincts = load_all_instincts(self.instincts_dir)
