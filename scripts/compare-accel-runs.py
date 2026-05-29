#!/usr/bin/env python3
"""Compare stored accelerator runs from results/index.csv.

Modes:
  accel       Accelerator A vs B (one best run per accelerator)
  sweep       One accelerator across a knob (op size / threads)
  regression  Same accelerator + config over time (by commit/date)
  topology    Thread-placement curves (same-CCD vs across-CCD, single vs SMT)
              pivoted by worker count, expanded from each run's sweep

Selection uses --filter tokens (key=val[,val...]) matched against index
columns, e.g.:
  compare-accel-runs.py --mode accel --filter op_size=65536
  compare-accel-runs.py --mode sweep --filter accelerator=memcpy
  compare-accel-runs.py --mode regression --filter accelerator=dsa config_hash=ab12cd34ef

Outputs MD/TXT/HTML + a comparison CSV under results/reports, with
delta-vs-baseline on the chosen --metric plus perf/W and perf/core columns.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _accel_common as store
from _lab_common import REPORTS
from _render import ascii_table, md_table, html_table

NUMERIC = {
    "throughput_gbps", "ops_per_sec", "latency_us_avg", "latency_us_p99",
    "cpu_pkg_w_avg", "dram_w_avg", "node_w_avg", "energy_j",
    "cores_to_saturate", "throughput_per_watt", "throughput_per_core",
    "threads", "op_size", "duration_sec",
}


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key, 0) or 0)
    except ValueError:
        return 0.0


def parse_filters(tokens: list[str]) -> dict[str, set[str]]:
    filt: dict[str, set[str]] = {}
    for tok in tokens:
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        filt[k.strip()] = {x.strip() for x in v.split(",") if x.strip()}
    return filt


def apply_filters(rows: list[dict], filt: dict[str, set[str]]) -> list[dict]:
    out = []
    for r in rows:
        if all(str(r.get(k, "")) in vals for k, vals in filt.items()):
            out.append(r)
    return out


def select(rows: list[dict], mode: str, metric: str) -> list[dict]:
    if mode == "accel":
        # best (max metric) run per accelerator
        best: dict[str, dict] = {}
        for r in rows:
            a = r["accelerator"]
            if a not in best or _num(r, metric) > _num(best[a], metric):
                best[a] = r
        return sorted(best.values(), key=lambda r: _num(r, metric), reverse=True)
    if mode == "sweep":
        return sorted(rows, key=lambda r: _num(r, "op_size"))
    if mode == "regression":
        return sorted(rows, key=lambda r: r.get("timestamp", ""))
    return rows


def build(rows: list[dict], metric: str, baseline: str) -> tuple[list[str], list[list]]:
    headers = ["run_id", "accelerator", "op_size", "threads", "tput_gbps",
               "lat_us", "pkg_w", "node_w", "sat_cores", "gbps/w", "gbps/core",
               f"d_{metric}%"]
    if not rows:
        return headers, []
    if baseline == "first":
        base_val = _num(rows[0], metric)
    else:
        match = next((r for r in rows if r["run_id"] == baseline), rows[0])
        base_val = _num(match, metric)

    out = []
    for r in rows:
        val = _num(r, metric)
        delta = round((val - base_val) / base_val * 100, 2) if base_val else 0.0
        out.append([
            r["run_id"], r["accelerator"], r.get("op_size", ""), r.get("threads", ""),
            r.get("throughput_gbps", ""), r.get("latency_us_avg", ""),
            r.get("cpu_pkg_w_avg", ""), r.get("node_w_avg", ""),
            r.get("cores_to_saturate", ""), r.get("throughput_per_watt", ""),
            r.get("throughput_per_core", ""), delta,
        ])
    return headers, out


def build_topology(rows: list[dict], metric: str) -> tuple[list[str], list[list]]:
    """Pivot placement strategies by worker count, expanded from run sweeps.

    The index keeps only the headline point per record, so the per-count curve
    is read from each run JSON's `sweep`. Rows group by (accel, op_size,
    placement); columns are worker counts. Lets same_ccd vs across_ccd and
    single_core vs smt_pair be read off directly.
    """
    groups: dict[tuple, dict[int, float]] = {}
    counts: set[int] = set()
    for r in rows:
        strategy = r.get("placement", "")
        if not strategy:
            continue  # non-topology run
        jp = r.get("json_path", "")
        if not jp:
            continue
        try:
            rec = store.load_run(store.REPO / jp)
        except (OSError, ValueError):
            continue
        key = (r.get("accelerator", ""), str(r.get("op_size", "")), strategy)
        g = groups.setdefault(key, {})
        for s in rec.get("sweep", []):
            t = int(s.get("threads", 0) or 0)
            v = float(s.get(metric, 0.0) or 0.0)
            counts.add(t)
            if t not in g or v > g[t]:
                g[t] = v
    ordered = sorted(counts)
    headers = ["accelerator", "op_size", "placement"] + [f"t{c}" for c in ordered]
    table = []
    for key in sorted(groups):
        g = groups[key]
        table.append([key[0], key[1], key[2]] + [g.get(c, "") for c in ordered])
    return headers, table


def main() -> int:
    ap = argparse.ArgumentParser(description="compare stored accelerator runs")
    ap.add_argument("--mode", choices=["accel", "sweep", "regression", "topology"],
                    default="accel")
    ap.add_argument("--filter", nargs="*", default=[], help="key=val[,val] selectors")
    ap.add_argument("--metric", default="throughput_gbps", help="primary metric for delta")
    ap.add_argument("--baseline", default="first", help="'first' or a run_id")
    args = ap.parse_args()

    rows = store.read_index()
    if not rows:
        print("results/index.csv is empty; run benchmarks first", file=sys.stderr)
        return 1

    rows = apply_filters(rows, parse_filters(args.filter))
    if args.mode != "topology":
        rows = select(rows, args.mode, args.metric)
    if not rows:
        print("no runs matched the filter", file=sys.stderr)
        return 1

    if args.mode == "topology":
        headers, table = build_topology(rows, args.metric)
        if not table:
            print("no topology runs matched (run: accel.py run --topology)", file=sys.stderr)
            return 1
    else:
        headers, table = build(rows, args.metric, args.baseline)
    ts = store.now_ts()
    title = f"Accelerator comparison ({args.mode}, metric={args.metric})"

    md = [f"# {title}", "",
          f"- **Generated (UTC):** {datetime.now(timezone.utc).isoformat()}",
          f"- **Runs compared:** {len(rows)}",
          f"- **Baseline:** {args.baseline}",
          f"- **Filter:** `{' '.join(args.filter) or 'none'}`",
          "", md_table(headers, table), ""]
    md_text = "\n".join(md) + "\n"

    txt = [title, "=" * len(title),
           f"Runs: {len(rows)}  Baseline: {args.baseline}  Filter: {' '.join(args.filter) or 'none'}",
           "", ascii_table(headers, table), ""]
    txt_text = "\n".join(txt) + "\n"

    html = ["<html><body style='font-family:sans-serif;font-size:14px'>",
            f"<h2>{title}</h2>",
            f"<p>Runs: {len(rows)} &nbsp; Baseline: {args.baseline} &nbsp; "
            f"Filter: {' '.join(args.filter) or 'none'}</p>",
            html_table(headers, table), "</body></html>"]
    html_text = "\n".join(html)

    REPORTS.mkdir(parents=True, exist_ok=True)
    base = REPORTS / f"comparison_{args.mode}_{ts}"
    (base.with_suffix(".md")).write_text(md_text, encoding="utf-8")
    (base.with_suffix(".txt")).write_text(txt_text, encoding="utf-8")
    (base.with_suffix(".html")).write_text(html_text, encoding="utf-8")
    with (base.with_suffix(".csv")).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(table)
    (REPORTS / ".latest_comparison").write_text(
        f"{base.with_suffix('.md')}\n{base.with_suffix('.txt')}\n"
        f"{base.with_suffix('.html')}\n{base.with_suffix('.csv')}\n", encoding="utf-8")

    print(f"Comparison MD:  {base.with_suffix('.md')}")
    print(f"Comparison CSV: {base.with_suffix('.csv')}")
    print("\n" + txt_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
