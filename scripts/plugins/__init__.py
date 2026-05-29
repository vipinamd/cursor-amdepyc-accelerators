"""Accelerator benchmark plugins.

Importing this package registers every plugin in plugins.base.REGISTRY
keyed by its tool name (matching the keys in config/workloads.json).
"""
from __future__ import annotations

from . import base  # noqa: F401

# Import side-effects register each plugin into base.REGISTRY.
from . import memcpy_ref  # noqa: F401,E402
from . import dma_perf    # noqa: F401,E402
from . import crypto_perf  # noqa: F401,E402
from . import eventdev    # noqa: F401,E402

from .base import REGISTRY, get_plugin  # noqa: F401,E402
