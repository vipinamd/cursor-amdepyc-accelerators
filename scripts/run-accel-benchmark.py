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
  python run-accel-benchmark.py --fix           # remediate setup-sanity blockers
  python run-accel-benchmark.py --skip-sanity   # bypass the preflight gate
  python run-accel-benchmark.py --accel dsa --topology  # topology placement sweep
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _accel_common as store
import _sanity as sanity_lib
import _topology as topology_lib
import _tuning as tuning_lib
from _lab_common import load_config, log, reboot_host, ssh_pass, wait_for_ssh
from plugins import get_plugin
from power import make_sampler
from profilers import make_profiler

# Tools whose worker threads are DPDK lcores, so topology pinning is meaningful.
TOPO_TOOLS = {"dma_perf", "crypto_perf"}

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


def _tuning_snapshot(cfg: dict, tuning_cfg: dict, host: str,
                     cache: dict) -> dict:
    """Capture a host-level tuning snapshot once per host (cached)."""
    if not tuning_cfg or not host or host == "local":
        return {}
    if host in cache:
        return cache[host]
    snap: dict = {}
    try:
        snap = tuning_lib.snapshot(
            cfg, tuning_cfg, host, cfg["SSH_USER"], ssh_pass(cfg, host), [])
        log(f"  platform tuning snapshot: {snap.get('family')} "
            f"-> {snap.get('verdict')} "
            f"({tuning_lib.diff_count(snap.get('checks', []))} diff vs guide)")
    except Exception as exc:  # noqa: BLE001 - tuning capture must not fail the run
        log(f"  platform tuning snapshot skipped ({type(exc).__name__})")
    cache[host] = snap
    return snap


def _setup_gate(cfg: dict, accel: str, accel_cfg: dict, tool: str, host: str,
                tuning_snap: dict, tuning_cfg: dict, sanity_cfg: dict,
                fix: bool) -> tuple[dict, dict, bool]:
    """Run the setup sanity preflight for one accelerator.

    Returns (setup_record, refreshed_tuning_snap, proceed). When a blocker
    remains (no --fix, or still blocked after remediation) proceed is False
    and the caller must skip this accelerator.
    """
    user, pw = cfg["SSH_USER"], ssh_pass(cfg, host)
    setup = sanity_lib.snapshot(cfg, sanity_cfg, accel, accel_cfg, tool,
                                host, user, pw, tuning_snap)
    nblock = sanity_lib.blocker_count(setup["rows"])
    log(f"  setup sanity: {setup['verdict']} ({nblock} blocker(s), "
        f"{sanity_lib.diff_count(setup['rows'])} issue(s))")
    if not setup["blocker"]:
        return setup, tuning_snap, True

    for r in setup["rows"]:
        if r["blocker"] and r["status"] == "FAIL":
            log(f"    BLOCKER {r['category']}/{r['item']}: "
                f"{r['remediation'] or 'see report'}")
    if not fix:
        log(f"  SKIP {accel}: setup blockers present (use --fix to remediate)")
        return setup, tuning_snap, False

    applied, reboot_needed = sanity_lib.remediate(
        setup["rows"], cfg, accel_cfg, tuning_snap, tuning_cfg, host, user, pw)
    if reboot_needed:
        log(f"  rebooting {host} to activate GRUB changes")
        reboot_host(host, user, pw)
        if not wait_for_ssh(host, user, pw):
            log(f"  {host} did not return within timeout")
        try:
            tuning_snap = tuning_lib.snapshot(cfg, tuning_cfg, host, user, pw, [])
        except Exception:  # noqa: BLE001 - best-effort refresh
            pass
    setup = sanity_lib.snapshot(cfg, sanity_cfg, accel, accel_cfg, tool,
                                host, user, pw, tuning_snap)
    setup["remediated"] = applied
    setup["rebooted"] = reboot_needed
    if setup["blocker"]:
        log(f"  SKIP {accel}: setup still blocked after --fix")
        return setup, tuning_snap, False
    log(f"  setup sanity after --fix: {setup['verdict']}")
    return setup, tuning_snap, True


def _topology_snapshot(cfg: dict, host: str, cache: dict) -> dict:
    """Capture the DUT CPU topology once per host (cached)."""
    if not host or host == "local":
        return {}
    if host in cache:
        return cache[host]
    snap: dict = {}
    try:
        snap = topology_lib.snapshot(cfg, host, cfg["SSH_USER"], ssh_pass(cfg, host))
        log(f"  topology: {snap.get('sockets')} socket(s), {snap.get('l3_count')} "
            f"L3 domain(s), {snap.get('cores_per_l3')} cores/CCD, "
            f"SMT {'on' if snap.get('smt') else 'off'}")
    except Exception as exc:  # noqa: BLE001 - topology capture must not fail the run
        log(f"  topology snapshot skipped ({type(exc).__name__})")
    cache[host] = snap
    return snap


def _execute_sweep(cfg: dict, accel: str, accel_cfg: dict, tool: str, plugin,
                   remote: bool, knobs: dict, host: str, tuning_snap: dict,
                   setup: dict, placement: dict, points: list[dict],
                   bundle_tag: str, eps: float) -> Path:
    """Run a list of sweep points into one stored run record.

    Each point is {"threads": int, "knobs": dict (extra per-point knobs),
    "meta": dict (per-point annotations such as lcores/cores/l3)}.
    """
    bundle = store.new_bundle(tag=bundle_tag)
    sampler = make_sampler(cfg, remote)
    profiler = make_profiler(cfg, remote)

    record = store.new_run_record(
        accelerator=accel, tool=tool, workload=tool, host=host,
        cpu_model=tuning_snap.get("model") or cfg.get("CPU_SOC", ""),
        soc=tuning_snap.get("family") or cfg.get("CPU_SOC", ""),
        knobs=knobs,
    )
    record["profile"]["profiler"] = profiler.name
    record["tuning"] = tuning_snap
    record["setup"] = setup
    record["placement"] = placement

    sampler.start()
    profiler.start(bundle)
    sweep: list[dict] = []
    raw_chunks: list[str] = []
    try:
        for p in points:
            threads = p["threads"]
            pknobs = {**knobs, **p.get("knobs", {})}
            label = p.get("meta", {}).get("lcores", threads)
            try:
                raw, m = plugin.run_point(cfg, accel_cfg, pknobs, threads)
            except Exception as exc:  # noqa: BLE001 - one bad point must not abort the run
                log(f"  {bundle_tag} threads={threads} FAILED ({type(exc).__name__}); "
                    f"recording empty point and continuing")
                raw = f"ERROR: {type(exc).__name__}: {exc}"
                m = {"throughput_gbps": 0.0, "ops_per_sec": 0.0,
                     "latency_us_avg": 0.0, "latency_us_p99": 0.0}
            raw_chunks.append(f"--- threads={threads} lcores={label} ---\n{raw}")
            sweep.append({"threads": threads, **m, **p.get("meta", {})})
            log(f"  {bundle_tag} threads={threads} "
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

    (bundle / "sweep_raw.log").write_text("\n\n".join(raw_chunks), encoding="utf-8")
    store.write_manifest(bundle, {
        "accelerator": accel, "tool": tool, "op_size": knobs.get("op_size"),
        "placement": placement.get("strategy", ""),
        "run_id": record["run_id"], "verdict": record["verdict"],
    })
    json_path = store.store_run(record)
    log(f"  stored {json_path.name} "
        f"(headline {perf['throughput_gbps']}Gbps, sat@{knee} cores, "
        f"{record['derived']['throughput_per_watt']} Gbps/W)")
    return json_path


def _topology_points(model: dict, strategy: str, counts: list[int],
                     numa: int, ctrl: int) -> tuple[list[dict], dict]:
    """Build sweep points + a placement summary for one strategy."""
    points: list[dict] = []
    seen: set[tuple] = set()
    lcores_by_count: dict[int, list[int]] = {}
    l3_all: set[int] = set()
    smt_any = False
    for c in counts:
        plan = topology_lib.plan_lcores(model, strategy, c, numa, exclude={ctrl})
        lcores = plan["lcores"]
        if not lcores or tuple(lcores) in seen:
            continue
        seen.add(tuple(lcores))
        lcores_by_count[len(lcores)] = lcores
        l3_all.update(plan["l3_domains"])
        smt_any = smt_any or plan["smt_used"]
        points.append({
            "threads": len(lcores),
            "knobs": {"worker_lcores": lcores, "ctrl_lcore": ctrl},
            "meta": {"lcores": lcores, "cores": plan["cores"],
                     "l3_domains": plan["l3_domains"], "smt": plan["smt_used"],
                     "note": plan["note"]},
        })
    placement = {
        "strategy": strategy,
        "numa_node": numa,
        "ctrl_lcore": ctrl,
        "l3_domains": sorted(l3_all),
        "smt_used": smt_any,
        "lcores_by_count": lcores_by_count,
    }
    return points, placement


def run_one(cfg: dict, accel: str, accel_cfg: dict, wl: dict,
            duration: int | None, max_threads: int | None,
            tuning_cfg: dict, tuning_cache: dict, sanity_cfg: dict,
            fix: bool, skip_sanity: bool, topology: bool,
            topology_cache: dict) -> list[Path]:
    tool = accel_cfg["tool"]
    plugin = get_plugin(tool)
    remote = bool(accel_cfg.get("remote", plugin.remote))
    eps = float(wl.get("saturation_epsilon", 0.05))

    sweep_threads = list(wl.get("thread_sweep", [1, 2, 4, 8]))
    if max_threads:
        sweep_threads = [t for t in sweep_threads if t <= max_threads] or [1]
    dur = duration or int(wl.get("duration_sec", 30))

    if topology and (tool not in TOPO_TOOLS or not remote):
        log(f"skip {accel}: --topology applies to DPDK lcore tools "
            f"({', '.join(sorted(TOPO_TOOLS))}) on the DUT, not '{tool}'")
        return []

    note = plugin.prepare(cfg, accel_cfg)
    log(f"=== {accel} ({tool}) === {note}")

    host = cfg.get("DUT_HOST", "local") if remote else "local"
    tuning_snap = _tuning_snapshot(cfg, tuning_cfg, host, tuning_cache)

    setup: dict = {}
    if remote and not skip_sanity and host and host != "local":
        setup, tuning_snap, proceed = _setup_gate(
            cfg, accel, accel_cfg, tool, host, tuning_snap, tuning_cfg,
            sanity_cfg, fix)
        if not proceed:
            return []

    def base_knobs(op_size) -> dict:
        k = dict(wl)
        k["op_size"] = op_size
        k["duration_sec"] = dur
        k.pop("thread_sweep", None)
        k.pop("topology", None)
        return k

    stored: list[Path] = []

    if topology:
        snap = _topology_snapshot(cfg, host, topology_cache)
        model = snap.get("model") or {}
        if not model.get("cpus"):
            log(f"skip {accel}: topology unavailable on {host}")
            return []
        topo_cfg = wl.get("topology") or {
            "strategies": list(topology_lib.STRATEGIES),
            "count_sweep": [1, 2, 4, 8, 16]}
        strategies = topo_cfg.get("strategies", list(topology_lib.STRATEGIES))
        count_sweep = topo_cfg.get("count_sweep", [1, 2, 4, 8, 16])
        if max_threads:
            count_sweep = [c for c in count_sweep if c <= max_threads] or [1]
        numa = int(accel_cfg.get("numa_node", cfg.get("BENCH_NUMA", "0")))
        ctrl = int(cfg.get("CTRL_LCORE", "1"))
        for op_size in _size_list(tool, wl):
            for strategy in strategies:
                counts = topology_lib.count_list(strategy, count_sweep)
                points, placement = _topology_points(model, strategy, counts, numa, ctrl)
                if not points:
                    log(f"  {accel} {op_size} {strategy}: no usable lcores, skipped")
                    continue
                knobs = base_knobs(op_size)
                knobs["placement"] = strategy
                stored.append(_execute_sweep(
                    cfg, accel, accel_cfg, tool, plugin, remote, knobs, host,
                    tuning_snap, setup, placement, points,
                    f"{accel}_{op_size}_{strategy}", eps))
        return stored

    for op_size in _size_list(tool, wl):
        knobs = base_knobs(op_size)
        points = [{"threads": t, "knobs": {}, "meta": {}} for t in sweep_threads]
        stored.append(_execute_sweep(
            cfg, accel, accel_cfg, tool, plugin, remote, knobs, host,
            tuning_snap, setup, {}, points, f"{accel}_{op_size}", eps))
    return stored


def main() -> int:
    ap = argparse.ArgumentParser(description="run accelerator benchmarks")
    ap.add_argument("--accel", help="comma-separated accelerator names (default: all enabled)")
    ap.add_argument("--duration", type=int, help="override per-point duration (s)")
    ap.add_argument("--max-threads", type=int, help="cap the thread sweep")
    ap.add_argument("--force-stubs", action="store_true",
                    help="also run scaffolded (unimplemented) tools")
    ap.add_argument("--fix", action="store_true",
                    help="remediate setup-sanity blockers (install/build/GRUB/hugepages/bind), reboot if needed, re-check")
    ap.add_argument("--skip-sanity", action="store_true",
                    help="skip the setup-sanity preflight gate")
    ap.add_argument("--topology", action="store_true",
                    help="topology placement sweep (single-core/SMT/same-CCD/across-CCD) for DPDK lcore tools")
    args = ap.parse_args()

    cfg = load_config()
    accelerators = store.load_accelerators()
    workloads = store.load_workloads()
    tuning_cfg = store.load_tuning()
    sanity_cfg = store.load_sanity()
    tuning_cache: dict = {}
    topology_cache: dict = {}

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
            run_one(cfg, name, accel_cfg, wl, args.duration, args.max_threads,
                    tuning_cfg, tuning_cache, sanity_cfg, args.fix, args.skip_sanity,
                    args.topology, topology_cache)
        )

    log(f"done: {len(all_stored)} run record(s) stored")
    return 0 if all_stored else 2


if __name__ == "__main__":
    sys.exit(main())
