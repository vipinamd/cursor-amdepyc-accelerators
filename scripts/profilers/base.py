#!/usr/bin/env python3
"""Profiler interface.

Wraps a benchmark window like the power sampler:
    profiler.start(bundle)   # begin sampling on the DUT
    ... run workload ...
    info = profiler.stop()   # finalize, fetch artifacts, parse hotspots

stop() returns:
    {profiler, artifacts: [bundle-relative names], hotspots: [{symbol, pct}]}
"""
from __future__ import annotations

from pathlib import Path


def empty_profile() -> dict:
    return {"profiler": "none", "artifacts": [], "hotspots": []}


class Profiler:
    name = "none"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bundle: Path | None = None

    def available(self) -> bool:
        return False

    def start(self, bundle: Path) -> None:
        self.bundle = bundle

    def stop(self) -> dict:
        return empty_profile()


class NoProfiler(Profiler):
    name = "none"

    def available(self) -> bool:
        return True
