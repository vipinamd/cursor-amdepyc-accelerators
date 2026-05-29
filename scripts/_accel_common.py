#!/usr/bin/env python3
"""Canonical metric schema and results-store helpers.

Every benchmark produces one *run record* (a nested dict) that is:
  1. written as pretty JSON under results/runs/<accelerator>/<run_id>.json
  2. flattened into one row appended to results/index.csv

The JSON keeps full detail (per-thread sweep, profile hotspots, raw config);
index.csv is the fast, git-diffable flat index the comparison engine reads.
Both are committed to git so historical runs are retrievable for comparison.
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from _lab_common import REPO, RESULTS, RUNS, REPORTS, BUNDLES, CONFIG

INDEX_CSV = RESULTS / "index.csv"
SCHEMA_VERSION = 1

# Flat columns mirrored into results/index.csv (order matters; keep stable).
INDEX_FIELDS = [
    "run_id",
    "timestamp",
    "git_commit",
    "host",
    "cpu_model",
    "soc",
    "accelerator",
    "tool",
    "workload",
    "config_hash",
    "threads",
    "op_size",
    "duration_sec",
    "throughput_gbps",
    "ops_per_sec",
    "latency_us_avg",
    "latency_us_p99",
    "cpu_pkg_w_avg",
    "dram_w_avg",
    "node_w_avg",
    "energy_j",
    "cores_to_saturate",
    "throughput_per_watt",
    "throughput_per_core",
    "placement",
    "verdict",
    "json_path",
]


# --------------------------------------------------------------------------
# time / identity helpers
# --------------------------------------------------------------------------
def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_commit() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def config_hash(knobs: dict) -> str:
    """Stable short hash of the workload knobs (excluding thread count).

    Two runs with the same config_hash are directly comparable for a
    regression-over-time view; differing only in threads keeps the same
    hash so the thread sweep stays grouped.
    """
    payload = json.dumps(knobs, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:10]


# --------------------------------------------------------------------------
# config (JSON) loading
# --------------------------------------------------------------------------
def _load_json_config(name: str) -> dict:
    """Load config/<name>.json, falling back to <name>.json.example."""
    real = CONFIG / name
    example = CONFIG / (name + ".example")
    path = real if real.exists() else example
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return data


def load_accelerators() -> dict:
    return _load_json_config("accelerators.json")


def load_workloads() -> dict:
    return _load_json_config("workloads.json")


def load_tuning() -> dict:
    return _load_json_config("amd-tuning.json")


def load_sanity() -> dict:
    return _load_json_config("sanity.json")


def enabled_accelerators() -> dict:
    return {k: v for k, v in load_accelerators().items() if v.get("enabled")}


# --------------------------------------------------------------------------
# run record construction
# --------------------------------------------------------------------------
def new_run_record(
    accelerator: str,
    tool: str,
    workload: str,
    host: str,
    cpu_model: str,
    soc: str,
    knobs: dict,
) -> dict:
    ts = now_ts()
    run_id = f"{accelerator}_{tool}_{ts}"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "timestamp": now_iso(),
        "ts_compact": ts,
        "git_commit": git_commit(),
        "host": host,
        "cpu_model": cpu_model,
        "soc": soc,
        "accelerator": accelerator,
        "tool": tool,
        "workload": workload,
        "config_hash": config_hash(knobs),
        "config": knobs,
        "sweep": [],          # list of {threads, throughput_gbps, ops_per_sec, ...}
        "metrics": {
            "performance": {
                "throughput_gbps": 0.0,
                "ops_per_sec": 0.0,
                "latency_us_avg": 0.0,
                "latency_us_p99": 0.0,
            },
            "power": {
                "source": "",
                "cpu_pkg_w_avg": 0.0,
                "cpu_pkg_w_peak": 0.0,
                "dram_w_avg": 0.0,
                "node_w_avg": 0.0,
                "node_w_peak": 0.0,
                "energy_j": 0.0,
            },
            "cpu": {
                "cores_used": 0,
                "cores_to_saturate": 0,
                "cpu_util_pct": 0.0,
                "offload_ratio": 0.0,
            },
        },
        "derived": {
            "throughput_per_watt": 0.0,
            "throughput_per_core": 0.0,
        },
        "profile": {
            "profiler": "",
            "artifacts": [],
            "hotspots": [],
        },
        "tuning": {},  # platform tuning snapshot (family, verdict, checks)
        "setup": {},   # setup sanity snapshot (verdict, blocker, rows, remediated)
        "placement": {},  # topology placement (strategy, lcores) when --topology
        "verdict": "UNKNOWN",
        "notes": [],
    }


def finalize_derived(record: dict) -> None:
    """Compute perf/W and perf/core from the populated metric fields."""
    perf = record["metrics"]["performance"]
    power = record["metrics"]["power"]
    cpu = record["metrics"]["cpu"]
    tput = perf.get("throughput_gbps", 0.0)

    # Prefer CPU package power for the offload-efficiency view; fall back to
    # node power so the metric is still defined when only BMC data exists.
    watt = power.get("cpu_pkg_w_avg", 0.0) or power.get("node_w_avg", 0.0)
    record["derived"]["throughput_per_watt"] = (
        round(tput / watt, 4) if watt > 0 else 0.0
    )
    cores = cpu.get("cores_used", 0)
    record["derived"]["throughput_per_core"] = (
        round(tput / cores, 4) if cores > 0 else 0.0
    )


# --------------------------------------------------------------------------
# store / index
# --------------------------------------------------------------------------
def _flatten(record: dict) -> dict:
    perf = record["metrics"]["performance"]
    power = record["metrics"]["power"]
    cpu = record["metrics"]["cpu"]
    cfg = record.get("config", {})
    return {
        "run_id": record["run_id"],
        "timestamp": record["timestamp"],
        "git_commit": record["git_commit"],
        "host": record["host"],
        "cpu_model": record["cpu_model"],
        "soc": record["soc"],
        "accelerator": record["accelerator"],
        "tool": record["tool"],
        "workload": record["workload"],
        "config_hash": record["config_hash"],
        "threads": cpu.get("cores_used", 0),
        "op_size": cfg.get("op_size", ""),
        "duration_sec": cfg.get("duration_sec", ""),
        "throughput_gbps": perf.get("throughput_gbps", 0.0),
        "ops_per_sec": perf.get("ops_per_sec", 0.0),
        "latency_us_avg": perf.get("latency_us_avg", 0.0),
        "latency_us_p99": perf.get("latency_us_p99", 0.0),
        "cpu_pkg_w_avg": power.get("cpu_pkg_w_avg", 0.0),
        "dram_w_avg": power.get("dram_w_avg", 0.0),
        "node_w_avg": power.get("node_w_avg", 0.0),
        "energy_j": power.get("energy_j", 0.0),
        "cores_to_saturate": cpu.get("cores_to_saturate", 0),
        "throughput_per_watt": record["derived"]["throughput_per_watt"],
        "throughput_per_core": record["derived"]["throughput_per_core"],
        "placement": record.get("placement", {}).get("strategy", ""),
        "verdict": record["verdict"],
        "json_path": "",  # filled in by store_run once the path is known
    }


def _migrate_index_header() -> None:
    """Rewrite results/index.csv if its header predates a new INDEX_FIELDS column.

    Reads existing rows (missing columns default to "") and writes them back
    with the current header, so adding a column stays backward-compatible.
    """
    if not INDEX_CSV.exists() or INDEX_CSV.stat().st_size < 5:
        return
    with INDEX_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == INDEX_FIELDS:
            return
        rows = list(reader)
    with INDEX_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in INDEX_FIELDS})


def store_run(record: dict) -> Path:
    """Write the per-run JSON, append the index row, update .latest_run."""
    finalize_derived(record)
    accel_dir = RUNS / record["accelerator"]
    accel_dir.mkdir(parents=True, exist_ok=True)
    json_path = accel_dir / f"{record['run_id']}.json"

    row = _flatten(record)
    row["json_path"] = str(json_path.relative_to(REPO).as_posix())

    json_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    _migrate_index_header()
    new_file = not INDEX_CSV.exists() or INDEX_CSV.stat().st_size < 5
    with INDEX_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)

    (RESULTS / ".latest_run").write_text(str(json_path) + "\n", encoding="utf-8")
    return json_path


def read_index() -> list[dict]:
    if not INDEX_CSV.exists():
        return []
    with INDEX_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_run(json_path: str | Path) -> dict:
    return json.loads(Path(json_path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# bundles (raw logs + profiler artifacts + manifest)
# --------------------------------------------------------------------------
def new_bundle(tag: str = "") -> Path:
    BUNDLES.mkdir(parents=True, exist_ok=True)
    name = f"bundle_{now_ts()}" + (f"_{tag}" if tag else "")
    bundle = BUNDLES / name
    bundle.mkdir(parents=True, exist_ok=True)
    return bundle


def write_manifest(bundle: Path, extra: dict) -> Path:
    manifest = {
        "bundle": bundle.name,
        "created": now_iso(),
        "git_commit": git_commit(),
        "files": sorted(
            p.name for p in bundle.iterdir()
            if p.is_file() and p.name != "manifest.json"
        ),
    }
    manifest.update(extra)
    mp = bundle / "manifest.json"
    mp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return mp
