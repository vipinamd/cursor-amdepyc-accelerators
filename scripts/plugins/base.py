#!/usr/bin/env python3
"""Plugin interface and registry for accelerator workloads.

A plugin maps one benchmark *tool* (dma_perf, crypto_perf, ...) to:
  - prepare():    one-time device/host setup before the sweep
  - run_point():  execute the workload at a given worker-thread count and
                  return (raw_log, metrics) for that single data point
  - parse():      turn raw tool output into a normalized metrics dict

Normalized metrics dict keys (all optional, default 0.0):
  throughput_gbps, ops_per_sec, latency_us_avg, latency_us_p99

Remote plugins (remote=True) execute on the DUT over SSH via _lab_common.
Local plugins (remote=False, e.g. the synthetic memcpy baseline) run
in-process on the orchestrator so the pipeline is demoable without hardware.
"""
from __future__ import annotations

from typing import Tuple

REGISTRY: dict[str, "AcceleratorPlugin"] = {}


def register(cls):
    """Class decorator: instantiate and register a plugin by its .name."""
    inst = cls()
    if not inst.name:
        raise ValueError(f"{cls.__name__} must set a non-empty .name")
    REGISTRY[inst.name] = inst
    return cls


def get_plugin(tool: str) -> "AcceleratorPlugin":
    if tool not in REGISTRY:
        raise KeyError(
            f"no plugin for tool '{tool}'. Registered: {sorted(REGISTRY)}"
        )
    return REGISTRY[tool]


def empty_metrics() -> dict:
    return {
        "throughput_gbps": 0.0,
        "ops_per_sec": 0.0,
        "latency_us_avg": 0.0,
        "latency_us_p99": 0.0,
    }


class AcceleratorPlugin:
    name: str = ""
    remote: bool = True
    # True if this tool requires real hardware/DPDK (a stub raises NotImplemented).
    implemented: bool = False

    def prepare(self, cfg: dict, accel_cfg: dict) -> str:
        """Optional one-time setup. Return a short human-readable note."""
        return ""

    def run_point(
        self, cfg: dict, accel_cfg: dict, knobs: dict, threads: int
    ) -> Tuple[str, dict]:
        """Run the workload with `threads` worker cores.

        Returns (raw_log, normalized_metrics). Default implementation is for
        SSH/remote DPDK tools: build a command, run it on the DUT, parse it.
        """
        from _lab_common import run_remote_script, ssh_pass

        host = cfg["DUT_HOST"]
        script = self.build_script(cfg, accel_cfg, knobs, threads)
        _, out = run_remote_script(
            host, cfg["SSH_USER"], ssh_pass(cfg, host), script,
            int(knobs.get("duration_sec", 30)) + 120,
        )
        return out, self.parse(out)

    def build_script(
        self, cfg: dict, accel_cfg: dict, knobs: dict, threads: int
    ) -> str:
        """Build the remote bash script that runs one data point."""
        raise NotImplementedError

    def parse(self, raw_log: str) -> dict:
        raise NotImplementedError
