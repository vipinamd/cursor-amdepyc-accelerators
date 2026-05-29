#!/usr/bin/env python3
"""DPDK crypto-perf runner for QAT (and other crypto devices).

Uses dpdk-test-crypto-perf in throughput mode. The thread-sweep point maps
to the number of worker lcores (--ptest throughput drives one core per
queue pair). We parse the summary line for throughput (Gbps) and the
ops/sec and cycle/latency figures the tool prints.
"""
from __future__ import annotations

import re

from .base import AcceleratorPlugin, register
from _lab_common import expand_home


@register
class CryptoPerf(AcceleratorPlugin):
    name = "crypto_perf"
    remote = True
    implemented = True

    def prepare(self, cfg: dict, accel_cfg: dict) -> str:
        bdf = accel_cfg.get("bdf", "")
        return f"crypto-perf on {accel_cfg.get('dpdk_driver', 'crypto')} {bdf}"

    def build_script(self, cfg: dict, accel_cfg: dict, knobs: dict, threads: int) -> str:
        dpdk = expand_home(cfg["DPDK_DIR"])
        build = cfg.get("DPDK_BUILD", "build")
        tool = f"{dpdk}/{build}/app/dpdk-test-crypto-perf"
        base = int(cfg.get("CTRL_LCORE", "1"))
        # control lcore + N worker lcores
        lcores = ",".join(str(base + i) for i in range(threads + 1))
        bdf = accel_cfg.get("bdf", "")
        devargs = accel_cfg.get("devargs", "")
        allow = f"-a {bdf}{(',' + devargs) if devargs else ''}" if bdf else ""
        buf = int(knobs.get("op_size", 1024))
        secs = int(knobs.get("duration_sec", 30))
        cipher = knobs.get("cipher_algo", "aes-cbc")
        auth = knobs.get("auth_algo", "sha2-256-hmac")
        devtype = accel_cfg.get("crypto_devtype", "crypto_qat")
        return f"""#!/bin/bash
{tool} -l {lcores} -n 4 {allow} -- \\
  --devtype {devtype} --ptest throughput \\
  --optype {knobs.get('optype', 'cipher-then-auth')} \\
  --cipher-algo {cipher} --cipher-op encrypt \\
  --auth-algo {auth} --auth-op generate \\
  --buffer-sz {buf} --total-ops 10000000 \\
  --burst-sz 32 --pool-sz 16384 2>&1 | tail -60 || true
"""

    def parse(self, raw_log: str) -> dict:
        metrics = {"throughput_gbps": 0.0, "ops_per_sec": 0.0, "latency_us_avg": 0.0, "latency_us_p99": 0.0}
        # crypto-perf prints Throughput in Gbps and Ops/s in its summary table.
        m = re.search(r"([\d.]+)\s*Gbps", raw_log)
        if m:
            metrics["throughput_gbps"] = float(m.group(1))
        o = re.search(r"Throughput.*?([\d.]+e?[+]?\d*)\s*$", raw_log, re.I | re.M)
        # Best-effort ops/sec: look for a large standalone float on the ops line.
        ops = re.search(r"Ops/s[^\d]*([\d.eE+]+)", raw_log)
        if ops:
            try:
                metrics["ops_per_sec"] = float(ops.group(1))
            except ValueError:
                pass
        return metrics
