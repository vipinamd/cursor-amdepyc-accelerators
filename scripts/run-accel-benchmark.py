#!/usr/bin/env python3
"""Run one or more accelerator benchmarks and store canonical run records.

For each selected accelerator and each configured operation size, the runner
performs a CPU-threads-to-saturate sweep (repeating the workload at each
worker-core count), wraps the sweep with a power sampler and a profiler,
finds the saturation knee, computes derived perf/W and perf/core, and writes
a per-run JSON + an index.csv row via _accel_common.store_run().

Usage:
  python run-accel-benchmark.py                 # all enabled accelerators
  python run-accel-benchmark.py --accel memcpy  # one accelerator
  python run-accel-benchmark.py --accel dsa,qat --duration 30
  python run-accel-benchmark.py --force-stubs   # also run scaffolded tools
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _accel_common as store
from _lab_common import load_config, log
from plugins import get_plugin
from power import make_sampler
from profilers import make_profiler

# Per-tool key that holds the list of operation/transfer sizes to test.
SIZE_KEYS = {
    "memcpy_ref": "buffer_bytes",
    "dma_perf": "transfer_sizes",
    "crypto_perf": "buffer_sizes",
    "eventdev": None,
}


def _size_list(tool: str, wl: dict) -> list:
    key = SIZE_KEYS.get(tool)
    if not key:
        return [0]
    sizes = wl.get(key) or [0]
    return list(sizes)


def _saturation(sweep: list[dict], eps: float) -> tuple[dict, int]:
    """Return (headline_point, cores_to_saturate).

    headline = max-throughput point; cores_to_saturate = the smallest worker
    count whose throughput is within eps of the maximum (the knee).
    """
    if not sweep:
        return {}, 0
    headline = max(sweep, key=lambda e: e["throughput_gbps"])
    max_t = headline["throughput_gbps"]
    knee = headline["threads"]
    if max_t > 0:
        for e in sorted(sweep, key=lambda e: e["threads"]):
            if e["throughput_gbps"] >= (1.0 - eps) * max_t:
                knee = e["threads"]
                break
    return headline, knee


def _offload_ratio(sweep: list[dict], headline: dict) -> float:
    """Scaling efficiency: (tput_max / tput_1thread) / cores_used.

    1.0 = perfect linear scaling with cores; <1.0 = sublinear (the engine
    needs proportionally more CPU threads to stay busy).
    """
    one = next((e for e in sweep if e["threads"] == 1), None)
    cores = headline.get("threads", 0)
    if not one or one["throughput_gbps"] <= 0 or cores <= 0:
        return 0.0
    return round((headline["throughput_gbps"] / one["throughput_gbps"]) / cores, 4)


def run_one(cfg: dict, accel: str, accel_cfg: dict, wl: dict,
            duration: int | None, max_threads: int | None) -> list[Path]:
    tool = accel_cfg["tool"]
    plugin = get_plugin(tool)
    remote = bool(accel_cfg.get("remote", plugin.remote))
    eps = float(wl.get("saturation_epsilon", 0.05))

    sweep_threads = list(wl.get("thread_sweep", [1, 2, 4, 8]))
    if max_threads:
        sweep_threads = [t for t in sweep_threads if t <= max_threads] or [1]
    dur = duration or int(wl.get("duration_sec", 30))

    note = plugin.prepare(cfg, accel_cfg)
    log(f"=== {accel} ({tool}) === {note}")

    stored: list[Path] = []
    for op_size in _size_list(tool, wl):
        knobs = dict(wl)
        knobs["op_size"] = op_size
        knobs["duration_sec"] = dur
        knobs.pop("thread_sweep", None)

        bundle = store.new_bundle(tag=f"{accel}_{op_size}")
        sampler = make_sampler(cfg, remote)
        profiler = make_profiler(cfg, remote)

        record = store.new_run_record(
            accelerator=accel, tool=tool, workload=tool,
            host=cfg.get("DUT_HOST", "local") if remote else "local",
            cpu_model=cfg.get("CPU_SOC", ""), soc=cfg.get("CPU_SOC", ""),
            knobs=knobs,
        )
        record["profile"]["profiler"] = profiler.name

        sampler.start()
        profiler.start(bundle)
        sweep: list[dict] = []
        raw_chunks: list[str] = []
        try:
            for threads in sweep_threads:
                raw, m = plugin.run_point(cfg, accel_cfg, knobs, threads)
                raw_chunks.append(f"--- threads={threads} ---\n{raw}")
                point = {"threads": threads, **m}
                sweep.append(point)
                log(f"  size={op_size} threads={threads} "
                    f"tput={m['throughput_gbps']}Gbps ops/s={m['ops_per_sec']}")
        finally:
            power = sampler.stop()
            prof_info = profiler.stop()

        headline, knee = _saturation(sweep, eps)
        record["sweep"] = sweep
        perf = record["metrics"]["performance"]
        perf["throughput_gbps"] = headline.get("throughput_gbps", 0.0)
        perf["ops_per_sec"] = headline.get("ops_per_sec", 0.0)
        perf["latency_us_avg"] = headline.get("latency_us_avg", 0.0)
        perf["latency_us_p99"] = headline.get("latency_us_p99", 0.0)
        record["metrics"]["power"] = power
        record["metrics"]["cpu"]["cores_used"] = headline.get("threads", 0)
        record["metrics"]["cpu"]["cores_to_saturate"] = knee
        record["metrics"]["cpu"]["offload_ratio"] = _offload_ratio(sweep, headline)
        record["profile"]["profiler"] = prof_info.get("profiler", profiler.name)
        record["profile"]["artifacts"] = prof_info.get("artifacts", [])
        record["profile"]["hotspots"] = prof_info.get("hotspots", [])
        record["verdict"] = "PASS" if perf["throughput_gbps"] > 0 else "FAIL"
        if power.get("source") == "synthetic":
            record["notes"].append("power values are synthetic (no hardware sensor)")

        # Persist the raw sweep log and a bundle manifest alongside the record.
        (bundle / "sweep_raw.log").write_text("\n\n".join(raw_chunks), encoding="utf-8")
        store.write_manifest(bundle, {
            "accelerator": accel, "tool": tool, "op_size": op_size,
            "run_id": record["run_id"], "verdict": record["verdict"],
        })

        json_path = store.store_run(record)
        stored.append(json_path)
        log(f"  stored {json_path.name} "
            f"(headline {perf['throughput_gbps']}Gbps, "
            f"sat@{knee} cores, {record['derived']['throughput_per_watt']} Gbps/W)")
    return stored


def main() -> int:
    ap = argparse.ArgumentParser(description="run accelerator benchmarks")
    ap.add_argument("--accel", help="comma-separated accelerator names (default: all enabled)")
    ap.add_argument("--duration", type=int, help="override per-point duration (s)")
    ap.add_argument("--max-threads", type=int, help="cap the thread sweep")
    ap.add_argument("--force-stubs", action="store_true",
                    help="also run scaffolded (unimplemented) tools")
    args = ap.parse_args()

    cfg = load_config()
    accelerators = store.load_accelerators()
    workloads = store.load_workloads()

    if args.accel:
        names = [n.strip() for n in args.accel.split(",") if n.strip()]
    else:
        names = [k for k, v in accelerators.items() if v.get("enabled")]
    if not names:
        log("no accelerators selected/enabled (see config/accelerators.json)")
        return 1

    all_stored: list[Path] = []
    for name in names:
        accel_cfg = accelerators.get(name)
        if not accel_cfg:
            log(f"unknown accelerator '{name}' (not in accelerators.json)")
            continue
        tool = accel_cfg.get("tool")
        plugin = get_plugin(tool)
        if not plugin.implemented and not args.force_stubs:
            log(f"skip {name}: tool '{tool}' is scaffolded (use --force-stubs to run)")
            continue
        wl = workloads.get(tool, {})
        all_stored.extend(
            run_one(cfg, name, accel_cfg, wl, args.duration, args.max_threads)
        )

    log(f"done: {len(all_stored)} run record(s) stored")
    return 0 if all_stored else 2


if __name__ == "__main__":
    sys.exit(main())
