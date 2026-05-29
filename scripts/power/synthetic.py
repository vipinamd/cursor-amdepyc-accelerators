#!/usr/bin/env python3
"""Synthetic power sampler for the no-hardware dry-run / demo path.

Produces a plausible, clearly-labelled CPU package wattage so the full
pipeline (and perf/W derived metric) is exercised on machines without RAPL
or a BMC (e.g. the Windows orchestrator running the memcpy_ref baseline).
Reports source='synthetic' so every report flags the values as modelled,
not measured.
"""
from __future__ import annotations

from .base import PowerSampler, empty_power


class SyntheticSampler(PowerSampler):
    source = "synthetic"

    # Modelled steady-state package/DRAM/node power for a busy EPYC socket.
    PKG_W = 145.0
    DRAM_W = 18.0
    NODE_W = 320.0

    def stop(self) -> dict:
        super().stop()
        p = empty_power()
        p["source"] = "synthetic"
        p["cpu_pkg_w_avg"] = self.PKG_W
        p["cpu_pkg_w_peak"] = round(self.PKG_W * 1.08, 3)
        p["dram_w_avg"] = self.DRAM_W
        p["node_w_avg"] = self.NODE_W
        p["node_w_peak"] = round(self.NODE_W * 1.05, 3)
        p["energy_j"] = round(self.PKG_W * self.elapsed, 3)
        return p
