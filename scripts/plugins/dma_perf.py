#!/usr/bin/env python3
"""DPDK dma-perf runner for DMA-class accelerators (DSA, SDXI, SDCI).

Uses dpdk-test-dma-perf, driven by an INI with a [GLOBAL] section (EAL args,
cache_flush, test_seconds) plus one [caseN] section. We generate one INI per
thread-sweep point and parse the average throughput from the result CSV.

Two modes (accel_cfg "mode"):
  dma  (default): DMA_MEM_COPY -- N worker lcores each drive a dmadev channel
                  on the configured device (hardware offload path).
  cpu           : CPU_MEM_COPY -- N worker lcores copy with the CPU (no
                  dmadev). On-platform DPDK baseline when no offload engine
                  is present.
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
        if accel_cfg.get("mode", "dma") == "cpu":
            return "dpdk-test-dma-perf CPU_MEM_COPY (software, no dmadev)"
        bdf = accel_cfg.get("bdf", "")
        return f"dma-perf on {accel_cfg.get('dpdk_driver', 'dmadev')} {bdf}"

    def _worker_lcores(self, cfg: dict, knobs: dict, threads: int) -> tuple[int, list[int]]:
        """Return (main_lcore, [worker_lcores]).

        Under --topology the runner supplies an explicit, topology-aware set via
        knobs; otherwise fall back to a contiguous range after the control lcore.
        """
        explicit = knobs.get("worker_lcores")
        if explicit:
            main = int(knobs.get("ctrl_lcore", cfg.get("CTRL_LCORE", "8")))
            return main, [int(w) for w in explicit]
        main = int(cfg.get("CTRL_LCORE", "8"))
        return main, [main + 1 + i for i in range(threads)]

    def _lcore_dma(self, accel_cfg: dict, workers: list[int]) -> str:
        bdf = accel_cfg.get("bdf", "0000:00:04.1")
        # New dma-perf format: one lcore_dmaN= line per worker channel.
        lines = []
        for i, w in enumerate(workers):
            lines.append(f"lcore_dma{i}=lcore={w},dev={bdf},dir=mem2mem")
        return "\n".join(lines)

    def build_script(self, cfg: dict, accel_cfg: dict, knobs: dict, threads: int) -> str:
        dpdk = expand_home(cfg["DPDK_DIR"])
        build = cfg.get("DPDK_BUILD", "build")
        tool = f"{dpdk}/{build}/app/dpdk-test-dma-perf"
        buf = int(knobs.get("op_size", 4096))
        ring = int(knobs.get("ring_size", 1024))
        secs = int(knobs.get("duration_sec", 30))
        numa = int(accel_cfg.get("numa_node", cfg.get("BENCH_NUMA", "0")))
        main, workers = self._worker_lcores(cfg, knobs, threads)
        wlist = ",".join(str(w) for w in workers)
        if accel_cfg.get("mode", "dma") == "cpu":
            # CPU_MEM_COPY: worker lcores copy with the CPU; no dmadev device.
            eal = f"-l {main},{wlist} --in-memory --no-pci --file-prefix=cpucopy"
            case = f"""[case1]
type=CPU_MEM_COPY
mem_size=10
buf_size={buf}
src_numa_node={numa}
dst_numa_node={numa}
lcore={wlist}
"""
        else:
            eal = f"-l {main},{wlist} --in-memory --file-prefix=dmaperf"
            case = f"""[case1]
type=DMA_MEM_COPY
mem_size=10
buf_size={buf}
dma_ring_size={ring}
kick_batch=32
src_numa_node={numa}
dst_numa_node={numa}
{self._lcore_dma(accel_cfg, workers)}
"""
        ini = f"""[GLOBAL]
eal_args={eal}
cache_flush=0
test_seconds={secs}

{case}"""
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
        metrics = {"throughput_gbps": 0.0, "ops_per_sec": 0.0,
                   "latency_us_avg": 0.0, "latency_us_p99": 0.0}

        # Aggregate bandwidth across workers: prefer the "Total Bandwidth"
        # line, then fall back to the CSV "Summary" row (Gbps is the
        # second-to-last column, MOps the last).
        m = re.search(r"Total Bandwidth:\s*([\d.]+)", raw_log, re.I)
        if m:
            metrics["throughput_gbps"] = float(m.group(1))
        mo = re.search(r"Total MOps:\s*([\d.]+)", raw_log, re.I)
        if mo:
            metrics["ops_per_sec"] = round(float(mo.group(1)) * 1e6, 2)

        if metrics["throughput_gbps"] == 0.0:
            for line in raw_log.splitlines():
                if "Summary" in line and "," in line:
                    cols = [c.strip() for c in line.split(",") if c.strip()]
                    nums = [c for c in cols if re.fullmatch(r"[\d.]+", c)]
                    if len(nums) >= 2:
                        metrics["throughput_gbps"] = float(nums[-2])
                        metrics["ops_per_sec"] = round(float(nums[-1]) * 1e6, 2)

        # Latency per op from average cycles/op and the reported CPU frequency.
        cyc = re.search(r"Cycles/op per worker:\s*([\d.]+)", raw_log, re.I)
        freq = re.search(r"Frequency:\s*([\d.]+)\s*Ghz", raw_log, re.I)
        if not freq:
            freq = re.search(r"CPU frequency,\s*([\d.]+)", raw_log, re.I)
        if cyc and freq and float(freq.group(1)) > 0:
            metrics["latency_us_avg"] = round(
                float(cyc.group(1)) / (float(freq.group(1)) * 1000.0), 4)
        return metrics
