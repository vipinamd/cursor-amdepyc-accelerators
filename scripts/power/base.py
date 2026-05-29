#!/usr/bin/env python3
"""Power sampler interface.

A sampler wraps a benchmark window:
    sampler.start()      # begin background sampling
    ... run workload ...
    metrics = sampler.stop()

`stop()` returns a normalized power metrics dict:
    source, cpu_pkg_w_avg, cpu_pkg_w_peak, dram_w_avg,
    node_w_avg, node_w_peak, energy_j
"""
from __future__ import annotations

import time


def empty_power() -> dict:
    return {
        "source": "",
        "cpu_pkg_w_avg": 0.0,
        "cpu_pkg_w_peak": 0.0,
        "dram_w_avg": 0.0,
        "node_w_avg": 0.0,
        "node_w_peak": 0.0,
        "energy_j": 0.0,
    }


def avg(values: list[float]) -> float:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


def peak(values: list[float]) -> float:
    vals = [v for v in values if v is not None]
    return round(max(vals), 3) if vals else 0.0


class PowerSampler:
    source = "none"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._t0 = 0.0
        self._t1 = 0.0

    def start(self) -> None:
        self._t0 = time.perf_counter()

    def stop(self) -> dict:
        self._t1 = time.perf_counter()
        return empty_power()

    @property
    def elapsed(self) -> float:
        end = self._t1 or time.perf_counter()
        return max(end - self._t0, 0.0)
