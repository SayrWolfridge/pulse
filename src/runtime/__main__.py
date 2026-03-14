"""
Pulse v2 Runtime — Entry Point
================================
Starts the HypostasRuntime as a long-running process.

Usage:
    python -m pulse.runtime

Handles SIGTERM / SIGINT for graceful shutdown so the LaunchAgent can
restart cleanly.

Logs to stdout/stderr (LaunchAgent redirects to log files).
"""

import logging
import signal
import sys
import time

from pulse.src.runtime import HypostasRuntime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger("pulse.runtime.__main__")


def main() -> None:
    runtime = HypostasRuntime()

    def _shutdown(signum: int, frame) -> None:  # noqa: ANN001
        logger.info("Received signal %d — shutting down gracefully …", signum)
        runtime.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    runtime.start()
    logger.info(
        "HypostasRuntime running | health: http://127.0.0.1:%d/runtime/health",
        HypostasRuntime.PORT,
    )

    # Keep the main thread alive while background threads do the work
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        runtime.stop()


if __name__ == "__main__":
    main()
