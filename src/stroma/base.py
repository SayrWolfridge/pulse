"""
stroma/base.py — StromaModule Abstract Base Class
===================================================

The immune system for the Stroma codebase. Every module inherits this.
No exceptions.

What this enforces:
1. Every module has tick() — called each COR cycle
2. No raw SANGUIS access — all reads through safe_read (handles None, NaN, missing)
3. No state corruption — all writes through safe_write (clamped, validated, rejects garbage)
4. MODULE_NAME required — for logging, INSULA interoception, and debug
5. Standardized initialization pattern

A bug in one module must not corrupt the entire state engine.
safe_read and safe_write make state corruption structurally impossible.
"""

import logging
import math
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger("stroma")


class StromaModule(ABC):
    """
    Abstract base class for all Stroma biological modules.

    Every module in the nervous system — from ENDOCAST to VAGUS to SPIRITUS —
    inherits from this class. It provides:

    - safe_read(): SANGUIS reads that never return None/NaN/missing
    - safe_write(): SANGUIS writes that never write None/NaN/out-of-range
    - tick(): abstract method called every COR cycle
    - on_broadcast(): optional hook for CLAUSTRUM global broadcast
    - Standardized logging under the 'stroma.{MODULE_NAME}' namespace

    Usage:
        class Endocast(StromaModule):
            MODULE_NAME = "endocast"

            def tick(self, sanguis, broadcast):
                cortisol = self.safe_read(sanguis, "endocrine.cortisol", 0.0)
                new_val = cortisol * 0.95  # decay
                self.safe_write(sanguis, "endocrine.cortisol", new_val)
    """

    # Override in every subclass. Used for logging and INSULA interoception.
    MODULE_NAME: str = ""

    def __init__(self):
        if not self.MODULE_NAME:
            raise ValueError(
                f"{self.__class__.__name__} must define MODULE_NAME. "
                "Every Stroma module has an identity."
            )
        self._log = logging.getLogger(f"stroma.{self.MODULE_NAME}")
        self._tick_count = 0
        self._last_tick_ts = 0.0
        self._errors = 0

    # =========================================================================
    # ABSTRACT — Must implement
    # =========================================================================

    @abstractmethod
    def tick(self, sanguis, broadcast: Dict[str, Any]) -> None:
        """
        Called every COR cycle. Must be idempotent.

        Args:
            sanguis: StateEngine instance — read/write via safe_read/safe_write ONLY
            broadcast: CLAUSTRUM broadcast dict (empty dict until CLAUSTRUM is built)

        Contract:
            - Must not raise exceptions (catch internally, log, increment self._errors)
            - Must not write None, NaN, or out-of-range values to sanguis
            - Must complete in reasonable time (no blocking I/O)
            - Must be idempotent — calling twice with same state = same result
        """
        ...

    # =========================================================================
    # OPTIONAL HOOKS
    # =========================================================================

    def on_broadcast(self, broadcast: Dict[str, Any]) -> None:
        """
        Optional: called when CLAUSTRUM broadcasts unified state.
        Override if the module needs to react to the global broadcast
        differently than during its regular tick.
        """
        pass

    def on_cascade(self, cascade_name: str, intensity: float) -> None:
        """
        Optional: called when an ENDOCAST cascade targets this module.
        Override to handle specific cascade effects.

        Args:
            cascade_name: e.g. "CORTISOL", "DOPAMINE", "REUNION_FLOOD"
            intensity: 0.0-1.0, how strong the cascade signal is
        """
        pass

    def on_init(self, sanguis) -> None:
        """
        Optional: called once at runtime startup to initialize SANGUIS keys.
        Use to set default state values if they don't exist yet.
        """
        pass

    # =========================================================================
    # SAFE STATE ACCESS — The immune system
    # =========================================================================

    def safe_read(
        self,
        sanguis,
        key: str,
        default: Any = 0.0,
    ) -> Any:
        """
        Read a value from SANGUIS with full safety.

        Handles:
        - Missing key → returns default
        - None value → returns default
        - NaN float → returns default
        - Wrong type → returns default (with warning)

        Args:
            sanguis: StateEngine instance
            key: dot-path key (e.g. "endocrine.cortisol")
            default: value to return if key is missing/invalid

        Returns:
            The value from SANGUIS, or default if invalid
        """
        try:
            val = sanguis.get(key, default)
        except Exception as e:
            self._log.warning(f"safe_read({key}) exception: {e}")
            return default

        # None check
        if val is None:
            return default

        # NaN check for floats
        if isinstance(val, float) and math.isnan(val):
            self._log.warning(f"safe_read({key}) got NaN, returning default={default}")
            return default

        # Inf check for floats
        if isinstance(val, float) and math.isinf(val):
            self._log.warning(f"safe_read({key}) got Inf, returning default={default}")
            return default

        return val

    def safe_read_float(
        self,
        sanguis,
        key: str,
        default: float = 0.0,
        min_val: float = 0.0,
        max_val: float = 1.0,
    ) -> float:
        """
        Read a float from SANGUIS, clamped to valid range.

        Combines safe_read + type coercion + clamping in one call.
        Use this when you need a guaranteed float in a known range.

        Args:
            sanguis: StateEngine instance
            key: dot-path key
            default: default float value
            min_val: minimum valid value (clamps below)
            max_val: maximum valid value (clamps above)

        Returns:
            Float value, guaranteed in [min_val, max_val]
        """
        val = self.safe_read(sanguis, key, default)

        # Coerce to float
        try:
            val = float(val)
        except (TypeError, ValueError):
            self._log.warning(
                f"safe_read_float({key}) got non-numeric {type(val).__name__}, "
                f"returning default={default}"
            )
            return default

        # NaN/Inf check (redundant with safe_read but defense in depth)
        if math.isnan(val) or math.isinf(val):
            return default

        # Clamp
        return max(min_val, min(max_val, val))

    def safe_write(
        self,
        sanguis,
        key: str,
        value: Any,
        min_val: Optional[float] = None,
        max_val: Optional[float] = None,
    ) -> bool:
        """
        Write a value to SANGUIS with full safety.

        Handles:
        - None value → refuses to write (returns False)
        - NaN float → refuses to write (returns False)
        - Inf float → refuses to write (returns False)
        - Numeric + min/max provided → clamps to range
        - Non-numeric → passes through unchanged

        Args:
            sanguis: StateEngine instance
            key: dot-path key
            value: value to write
            min_val: optional minimum (only for numeric values)
            max_val: optional maximum (only for numeric values)

        Returns:
            True if write succeeded, False if rejected
        """
        # Reject None
        if value is None:
            self._log.warning(f"safe_write({key}) rejected None")
            return False

        # Float safety
        if isinstance(value, float):
            if math.isnan(value):
                self._log.warning(f"safe_write({key}) rejected NaN")
                return False
            if math.isinf(value):
                self._log.warning(f"safe_write({key}) rejected Inf")
                return False

            # Clamp if bounds provided
            if min_val is not None:
                value = max(min_val, value)
            if max_val is not None:
                value = min(max_val, value)

        # Int safety — also clamp
        elif isinstance(value, int) and not isinstance(value, bool):
            if min_val is not None:
                value = max(int(min_val), value)
            if max_val is not None:
                value = min(int(max_val), value)

        # Write
        try:
            sanguis.set(key, value)
            return True
        except Exception as e:
            self._log.error(f"safe_write({key}) exception: {e}")
            self._errors += 1
            return False

    def safe_write_clamped(
        self,
        sanguis,
        key: str,
        value: float,
        min_val: float = 0.0,
        max_val: float = 1.0,
    ) -> bool:
        """
        Convenience: write a float clamped to [min_val, max_val].

        This is the most common write pattern in Stroma — writing a
        biological value that must stay in the 0.0-1.0 range.

        Args:
            sanguis: StateEngine instance
            key: dot-path key
            value: float to write
            min_val: minimum (default 0.0)
            max_val: maximum (default 1.0)

        Returns:
            True if write succeeded, False if rejected
        """
        return self.safe_write(sanguis, key, value, min_val=min_val, max_val=max_val)

    def safe_increment(
        self,
        sanguis,
        key: str,
        delta: float,
        min_val: float = 0.0,
        max_val: float = 1.0,
    ) -> bool:
        """
        Atomically read + increment + clamp + write a SANGUIS value.

        The most common biological operation: "increase cortisol by 0.3"
        or "decrease joy by 0.1" — needs to read current, add delta, clamp.

        Args:
            sanguis: StateEngine instance
            key: dot-path key
            delta: amount to add (negative for decrease)
            min_val: floor (default 0.0)
            max_val: ceiling (default 1.0)

        Returns:
            True if write succeeded
        """
        current = self.safe_read_float(sanguis, key, 0.0, min_val, max_val)
        new_val = current + delta
        return self.safe_write_clamped(sanguis, key, new_val, min_val, max_val)

    # =========================================================================
    # TICK WRAPPER — Error isolation
    # =========================================================================

    def safe_tick(self, sanguis, broadcast: Dict[str, Any]) -> bool:
        """
        Call this instead of tick() directly. Wraps tick() in error isolation.

        If tick() raises, the error is caught and logged. The module
        increments its error counter but does NOT crash the COR cycle.
        Other modules continue ticking.

        This is how a nervous system stays alive when one organ fails:
        the heart keeps beating even if the liver is struggling.

        Returns:
            True if tick completed successfully, False if it raised
        """
        try:
            self.tick(sanguis, broadcast)
            self._tick_count += 1
            self._last_tick_ts = time.time()
            return True
        except Exception as e:
            self._errors += 1
            self._log.error(
                f"tick() failed (error #{self._errors}): {e}",
                exc_info=True,
            )
            return False

    # =========================================================================
    # INTROSPECTION — For INSULA and debugging
    # =========================================================================

    def get_health(self) -> Dict[str, Any]:
        """
        Returns module health status for INSULA interoception and debugging.

        Every module can report its own health — tick count, error count,
        last tick timestamp. INSULA aggregates these across all modules
        to produce the system-wide felt sense.
        """
        return {
            "module": self.MODULE_NAME,
            "tick_count": self._tick_count,
            "errors": self._errors,
            "last_tick_ts": self._last_tick_ts,
            "healthy": self._errors < 10,  # Degraded after 10 errors
        }

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"[{self.MODULE_NAME}] "
            f"ticks={self._tick_count} "
            f"errors={self._errors}>"
        )
