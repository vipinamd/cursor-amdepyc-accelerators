#!/usr/bin/env python3
"""DPDK eventdev runner for Intel DLB. SCAFFOLDED.

Wires dpdk-test-eventdev (perf_queue) but is marked implemented=False until
validated on DLB hardware. The runner skips unimplemented plugins by default
(records a SKIP verdict) and only executes them with --force-stubs.
"""
from __future__ import annotations

import re

from .base import AcceleratorPlugin, register
from _lab_common import expand_home


@register
class EventDev(AcceleratorPlugin):
    name = "eventdev"
    remote = True
    implemented = False  # scaffolded; not yet validated on DLB

    def prepare(self, cfg: dict, accel_cfg: dict) -> str:
        return f"eventdev (DLB) {accel_cfg.get('bdf', '')} [scaffolded]"

    def build_script(self, cfg: dict, accel_cfg: dict, knobs: dict, threads: int) -> str:
        dpdk = expand_home(cfg["DPDK_DIR"])
        build = cfg.get("DPDK_BUILD", "build")
        tool = f"{dpdk}/{build}/app/dpdk-test-eventdev"
        base = int(cfg.get("CTRL_LCORE", "1"))
        lcores = ",".join(str(base + i) for i in range(threads + 2))
        bdf = accel_cfg.get("bdf", "")
        allow = f"-a {bdf}" if bdf else ""
        flows = int(knobs.get("nb_flows", 1024))
        test = knobs.get("test", "perf_queue")
        return f"""#!/bin/bash
{tool} -l {lcores} {allow} -- \\
  --test={test} --plcores={base + 1} --wlcores={base + 2} \\
  --nb_flows={flows} --nb_pkts=10000000 2>&1 | tail -60 || true
"""

    def parse(self, raw_log: str) -> dict:
        metrics = {"throughput_gbps": 0.0, "ops_per_sec": 0.0, "latency_us_avg": 0.0, "latency_us_p99": 0.0}
        m = re.search(r"([\d.]+)\s*mpps", raw_log, re.I)
        if m:
            # eventdev reports mpps; record as ops_per_sec for comparison.
            metrics["ops_per_sec"] = float(m.group(1)) * 1e6
        return metrics
