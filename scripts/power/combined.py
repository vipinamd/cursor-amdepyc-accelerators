#!/usr/bin/env python3
"""Factory + combiner that selects power backend(s) from config.

POWER_SOURCE values: rapl | bmc | both | synthetic | none.
Local (non-remote) workloads always fall back to the synthetic sampler
because there is no DUT to read RAPL/BMC from.
"""
from __future__ import annotations

from .base import PowerSampler, empty_power
from .synthetic import SyntheticSampler
from .rapl import RaplSampler
from .bmc import BmcSampler


class CombinedSampler(PowerSampler):
    source = "both"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._rapl = RaplSampler(cfg)
        self._bmc = BmcSampler(cfg)

    def start(self) -> None:
        super().start()
        self._rapl.start()
        self._bmc.start()

    def stop(self) -> dict:
        super().stop()
        r = self._rapl.stop()
        b = self._bmc.stop()
        p = empty_power()
        p["source"] = "rapl+bmc"
        p["cpu_pkg_w_avg"] = r["cpu_pkg_w_avg"]
        p["cpu_pkg_w_peak"] = r["cpu_pkg_w_peak"]
        p["dram_w_avg"] = r["dram_w_avg"]
        p["energy_j"] = r["energy_j"]
        p["node_w_avg"] = b["node_w_avg"]
        p["node_w_peak"] = b["node_w_peak"]
        return p


def make_sampler(cfg: dict, remote: bool) -> PowerSampler:
    if not remote:
        return SyntheticSampler(cfg)
    source = (cfg.get("POWER_SOURCE", "both") or "both").lower()
    if source == "rapl":
        return RaplSampler(cfg)
    if source == "bmc":
        return BmcSampler(cfg)
    if source == "synthetic":
        return SyntheticSampler(cfg)
    if source == "none":
        return PowerSampler(cfg)
    return CombinedSampler(cfg)
