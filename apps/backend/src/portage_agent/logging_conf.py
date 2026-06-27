"""Tiny logging setup — line-oriented logs to stdout (so `docker logs` shows them)."""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _CONFIGURED = True
