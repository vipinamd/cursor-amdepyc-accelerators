#!/usr/bin/env python3
"""DPDK dma-perf runner for DMA-class accelerators (DSA, SDXI, SDCI).

Uses dpdk-test-dma-perf, which is driven by an INI config describing the
test case and the lcore->DMA-channel mapping. We generate one INI per
thread-sweep point (N worker lcores each driving a DMA channel on the
configured device), run the tool to a result CSV on the DUT, and parse the
average throughput out of that CSV.

For a CPU-vs-offload baseline the same tool supports type=CPU_MEM_COPY; the
framework's memcpy_ref plugin already provides a portable software baseline,
so this plugin focuses on the DMA_MEM_COPY (hardware offload) path.
"""
from __future__ import annotations

import re

from .base import AcceleratorPlugin, register
from _lab_common import expand_home


@register
class DmaPerf(AcceleratorPlugin):
    name = "dma_perf"
    remote = True
    implemented = True

    def prepare(self, cfg: dict, accel_cfg: dict) -> str:
        bdf = accel_cfg.get("bdf", "")
        return f"dma-perf on {accel_cfg.get('dpdk_driver', 'dmadev')} {bdf}"

    def _lcore_dma(self, cfg: dict, accel_cfg: dict, threads: int) -> str:
        base = int(cfg.get("CTRL_LCORE", "1"))
        bdf = accel_cfg.get("bdf", "0000:00:04.1")
        devargs = accel_cfg.get("devargs", "")
        dev = f"{bdf}{(',' + devargs) if devargs else ''}"
        entries = [f"lcore{base + 1 + i}@{dev}" for i in range(threads)]
        return ", ".join(entries)

    def build_script(self, cfg: dict, accel_cfg: dict, knobs: dict, threads: int) -> str:
        dpdk = expand_home(cfg["DPDK_DIR"])
        build = cfg.get("DPDK_BUILD", "build")
        tool = f"{dpdk}/{build}/app/dpdk-test-dma-perf"
        buf = int(knobs.get("op_size", 4096))
        ring = int(knobs.get("ring_size", 1024))
        secs = int(knobs.get("duration_sec", 30))
        lcore_dma = self._lcore_dma(cfg, accel_cfg, threads)
        ini = f"""[case1]
type=DMA_MEM_COPY
mem_size=10
buf_size={buf}
dma_ring_size={ring}
kick_batch=32
src_numa_node=0
dst_numa_node=0
cache_flush=0
test_seconds={secs}
lcore_dma={lcore_dma}
eal_args=--in-memory --file-prefix=dmaperf
"""
        cfg_path = f"/tmp/dma_perf_{threads}.ini"
        res_path = f"/tmp/dma_perf_{threads}_result.csv"
        return f"""#!/bin/bash
cat > {cfg_path} <<'EOF'
{ini}EOF
{tool} --config={cfg_path} --result={res_path} 2>&1 | tail -40 || true
echo '----RESULT-CSV----'
cat {res_path} 2>/dev/null || echo NO_RESULT
"""

    def parse(self, raw_log: str) -> dict:
        metrics = {"throughput_gbps": 0.0, "ops_per_sec": 0.0, "latency_us_avg": 0.0, "latency_us_p99": 0.0}
        # dma-perf result CSV/header carries an average throughput in Gbps.
        m = re.search(r"Average\s+Throughput.*?([\d.]+)", raw_log, re.I)
        if not m:
            m = re.search(r"throughput.*?Gbps[^\d]*([\d.]+)", raw_log, re.I)
        if m:
            metrics["throughput_gbps"] = float(m.group(1))
        return metrics
