#!/usr/bin/env python3
"""Analyze a stored accelerator run and write an executive summary.

Reads the latest run record (or one given with --run), renders an executive
summary (MD + TXT + HTML) covering performance, power, CPU-threads-to-saturate
and offload efficiency, profiler hotspots, a verdict and next steps, plus a
short table of recent runs from the index for context.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _accel_common as store
from _lab_common import RESULTS, REPORTS
from _render import ascii_table, md_table, html_table


def _latest_run_path(arg: str | None) -> Path | None:
    if arg:
        return Path(arg)
    marker = RESULTS / ".latest_run"
    if marker.exists():
        return Path(marker.read_text(encoding="utf-8").strip())
    runs = sorted(store.RUNS.glob("*/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None


def _next_steps(rec: dict) -> list[str]:
    steps = []
    perf = rec["metrics"]["performance"]
    cpu = rec["metrics"]["cpu"]
    if perf["throughput_gbps"] <= 0:
        steps.append("No throughput recorded - verify device bind (vfio-pci), hugepages, and devargs in config/accelerators.json.")
    if rec["metrics"]["power"].get("source") == "synthetic":
        steps.append("Power is synthetic; run on the DUT with POWER_SOURCE=both for measured RAPL + BMC watts.")
    if cpu["cores_to_saturate"] and cpu["cores_to_saturate"] == cpu["cores_used"]:
        steps.append("Engine did not saturate within the thread sweep; raise MAX_WORKER_LCORES / thread_sweep to find the knee.")
    if not rec["profile"]["hotspots"] and rec["profile"]["profiler"] not in ("none", ""):
        steps.append("Profiler produced no hotspots; confirm perf/uProf/VTune is installed and permitted on the DUT.")
    steps.append("Compare against other engines: python scripts/compare-accel-runs.py --mode accel")
    return steps


def render(rec: dict, recent: list[dict]) -> tuple[str, str, str, str, str]:
    ts = store.now_ts()
    verdict = rec["verdict"]
    perf = rec["metrics"]["performance"]
    power = rec["metrics"]["power"]
    cpu = rec["metrics"]["cpu"]
    der = rec["derived"]

    overview = [
        ["Accelerator", rec["accelerator"]],
        ["Tool / workload", f"{rec['tool']}"],
        ["Host / SoC", f"{rec['host']} / {rec['soc']}"],
        ["Op size", rec["config"].get("op_size", "")],
        ["Verdict", verdict],
        ["Git commit", rec["git_commit"] or "n/a"],
        ["Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")],
    ]
    perf_hdr = ["Throughput (Gbps)", "Ops/s", "Lat avg (us)", "Lat p99 (us)"]
    perf_row = [perf["throughput_gbps"], perf["ops_per_sec"], perf["latency_us_avg"], perf["latency_us_p99"]]
    power_hdr = ["Source", "Pkg W avg", "Pkg W peak", "DRAM W", "Node W avg", "Energy (J)"]
    power_row = [power.get("source", ""), power["cpu_pkg_w_avg"], power["cpu_pkg_w_peak"],
                 power["dram_w_avg"], power["node_w_avg"], power["energy_j"]]
    eff_hdr = ["Cores used", "Cores to saturate", "Offload ratio", "Gbps/W", "Gbps/core"]
    eff_row = [cpu["cores_used"], cpu["cores_to_saturate"], cpu["offload_ratio"],
               der["throughput_per_watt"], der["throughput_per_core"]]

    sweep_hdr = ["Threads", "Throughput (Gbps)", "Ops/s", "Lat avg (us)"]
    sweep_rows = [[s["threads"], s["throughput_gbps"], s["ops_per_sec"], s.get("latency_us_avg", 0.0)]
                  for s in rec.get("sweep", [])]

    hot = rec["profile"]["hotspots"][:8]
    hot_hdr = ["Symbol", "%"]
    hot_rows = [[h["symbol"], h["pct"]] for h in hot]

    recent_hdr = ["Run", "Accel", "Tput Gbps", "Sat cores", "Gbps/W"]
    recent_rows = [[r["run_id"], r["accelerator"], r["throughput_gbps"],
                    r["cores_to_saturate"], r["throughput_per_watt"]] for r in recent[:8]]

    steps = _next_steps(rec)

    # ----- Markdown -----
    md = [f"# Accelerator run summary: {rec['accelerator']} ({rec['tool']})", "",
          f"- **Verdict:** **{verdict}**",
          f"- **Run ID:** `{rec['run_id']}`",
          f"- **Generated (UTC):** {datetime.now(timezone.utc).isoformat()}",
          "", "## Overview", "", md_table(["Item", "Value"], overview),
          "", "## Performance", "", md_table(perf_hdr, [perf_row]),
          "", "## Power", "", md_table(power_hdr, [power_row]),
          "", "## CPU efficiency", "", md_table(eff_hdr, [eff_row]),
          "", "## Thread-to-saturate sweep", "", md_table(sweep_hdr, sweep_rows)]
    if hot_rows:
        md += ["", "## Profiler hotspots ("+rec["profile"]["profiler"]+")", "", md_table(hot_hdr, hot_rows)]
    if rec.get("notes"):
        md += ["", "## Notes", ""] + [f"- {n}" for n in rec["notes"]]
    md += ["", "## Recent runs", "", md_table(recent_hdr, recent_rows) if recent_rows else "_none_"]
    md += ["", "## Next steps", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    md_text = "\n".join(md) + "\n"

    # ----- TXT -----
    txt = [f"Accelerator run summary: {rec['accelerator']} ({rec['tool']})",
           "=" * 50, f"Verdict: {verdict}   Run: {rec['run_id']}", "",
           "OVERVIEW", ascii_table(["Item", "Value"], overview), "",
           "PERFORMANCE", ascii_table(perf_hdr, [perf_row]), "",
           "POWER", ascii_table(power_hdr, [power_row]), "",
           "CPU EFFICIENCY", ascii_table(eff_hdr, [eff_row]), "",
           "THREAD-TO-SATURATE SWEEP", ascii_table(sweep_hdr, sweep_rows)]
    if hot_rows:
        txt += ["", f"PROFILER HOTSPOTS ({rec['profile']['profiler']})", ascii_table(hot_hdr, hot_rows)]
    txt += ["", "NEXT STEPS", "-" * 40]
    txt += [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
    txt_text = "\n".join(txt) + "\n"

    # ----- HTML -----
    color = {"PASS": "#0a0", "FAIL": "#c00", "SKIP": "#888", "UNKNOWN": "#888"}.get(verdict, "#000")
    html = ["<html><body style='font-family:sans-serif;font-size:14px'>",
            f"<h2>Accelerator run summary: {rec['accelerator']} ({rec['tool']})</h2>",
            f"<p><b>Verdict:</b> <span style='color:{color}'>{verdict}</span> "
            f"&nbsp; <b>Run:</b> {rec['run_id']}</p>",
            "<h3>Overview</h3>", html_table(["Item", "Value"], overview),
            "<h3>Performance</h3>", html_table(perf_hdr, [perf_row]),
            "<h3>Power</h3>", html_table(power_hdr, [power_row]),
            "<h3>CPU efficiency</h3>", html_table(eff_hdr, [eff_row]),
            "<h3>Thread-to-saturate sweep</h3>", html_table(sweep_hdr, sweep_rows)]
    if hot_rows:
        html += [f"<h3>Profiler hotspots ({rec['profile']['profiler']})</h3>", html_table(hot_hdr, hot_rows)]
    if recent_rows:
        html += ["<h3>Recent runs</h3>", html_table(recent_hdr, recent_rows)]
    html += ["<h3>Next steps</h3><ol>"] + [f"<li>{s}</li>" for s in steps] + ["</ol></body></html>"]
    html_text = "\n".join(html)

    return md_text, txt_text, html_text, ts, verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="path to a specific run JSON")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--enrich", action="store_true", help="print TXT summary to stdout")
    args = ap.parse_args()

    run_path = _latest_run_path(args.run)
    if not run_path or not run_path.exists():
        print("no run record found; run a benchmark first", file=sys.stderr)
        return 1
    rec = store.load_run(run_path)
    recent = list(reversed(store.read_index()))

    md, txt, html, ts, verdict = render(rec, recent)
    REPORTS.mkdir(parents=True, exist_ok=True)
    md_path = REPORTS / f"accel_summary_{ts}.md"
    txt_path = REPORTS / f"accel_summary_{ts}.txt"
    html_path = REPORTS / f"accel_summary_{ts}.html"
    md_path.write_text(md, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    (REPORTS / ".latest_summary").write_text(
        f"{md_path}\n{txt_path}\n{html_path}\n{verdict}\n", encoding="utf-8")

    print(f"Summary MD:   {md_path}")
    print(f"Summary TXT:  {txt_path}")
    print(f"Summary HTML: {html_path}")
    print(f"Verdict:      {verdict}")
    if args.enrich:
        print("\n" + txt)
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
