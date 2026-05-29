# Framework architecture

The framework separates four concerns so each can evolve independently:

```
accel.py (orchestrator)
   |
   |-- run-accel-benchmark.py        per (accelerator, op_size): thread sweep
   |      |-- plugins/<tool>          WHAT to run + HOW to parse it
   |      |-- power/<source>          measure watts/energy over the window
   |      |-- profilers/<backend>     sample hotspots over the window
   |      \-- _accel_common.store_run write JSON record + index.csv row
   |
   |-- analyze-accel-run.py          one run  -> executive summary (MD/TXT/HTML)
   \-- compare-accel-runs.py         many runs -> comparison (MD/TXT/HTML/CSV)
```

## Plugins (scripts/plugins/)

A plugin maps one benchmark *tool* to three methods (see `plugins/base.py`):

- `prepare(cfg, accel_cfg)` - one-time device/host setup, returns a note
- `run_point(cfg, accel_cfg, knobs, threads)` - run one data point, return `(raw_log, metrics)`
- `parse(raw_log)` - normalize tool output into `{throughput_gbps, ops_per_sec, latency_us_avg, latency_us_p99}`

Plugins declare `remote` (SSH to the DUT) and `implemented`. Scaffolded
plugins (`implemented=False`, e.g. `eventdev`/DLB) are skipped unless you pass
`--force-stubs`. Accelerators are bound to a tool in `config/accelerators.json`,
so several engines can share one runner (DSA, SDXI, SDCI all use `dma_perf`).

## Thread-to-saturate sweep

`run-accel-benchmark.py` repeats the workload at each worker-core count in
`thread_sweep`. The **saturation knee** is the smallest core count whose
throughput is within `saturation_epsilon` of the maximum. The **offload
ratio** is `(throughput_max / throughput_1thread) / cores_used` - 1.0 means
perfect linear scaling, lower means the engine needs proportionally more CPU
threads to stay busy.

Power and the profiler wrap the entire sweep for a run, so energy and hotspots
reflect the full measured window.

## Run record schema

Each run is one JSON file under `results/runs/<accelerator>/<run_id>.json`:

- identity: `run_id`, `timestamp`, `git_commit`, `host`, `cpu_model`, `soc`
- selection: `accelerator`, `tool`, `workload`, `config_hash`
- `config`: the workload knobs (op size, duration, ring size, ...)
- `sweep`: per-thread-count data points
- `metrics.performance`: throughput_gbps, ops_per_sec, latency_us_avg/p99
- `metrics.power`: source, cpu_pkg_w_avg/peak, dram_w_avg, node_w_avg/peak, energy_j
- `metrics.cpu`: cores_used, cores_to_saturate, cpu_util_pct, offload_ratio
- `derived`: throughput_per_watt, throughput_per_core
- `profile`: profiler, artifacts, hotspots
- `verdict`, `notes`

`config_hash` is a stable hash of the knobs (excluding thread count), so runs
with the same configuration line up for a regression-over-time view.

## Results store

- `results/runs/**.json` - full detail, committed to git
- `results/index.csv` - one flattened row per run, committed to git; this is
  what `compare-accel-runs.py` reads, and it is intentionally diff-friendly
- `results/bundles/bundle_*/` - raw logs + profiler artifacts + `manifest.json`
  (gitignored; large and machine-specific)
- `results/reports/` - generated summaries and comparisons
- markers (`.latest_run`, `.latest_summary`, `.latest_comparison`) are gitignored

Because the JSON and index are committed, historical runs are retrievable for
comparison long after the lab session ends.
