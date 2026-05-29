# Comparison engine

`compare-accel-runs.py` reads `results/index.csv`, filters and selects runs,
and writes a comparison report (MD/TXT/HTML + CSV) with delta-vs-baseline on
the chosen metric plus perf/W and perf/core columns.

```bash
python scripts/compare-accel-runs.py --mode <accel|sweep|regression|topology> \
    --filter key=val[,val] ... --metric throughput_gbps --baseline first
```

## Modes

| Mode | What it shows | Selection |
|------|----------------|-----------|
| `accel` | Accelerator A vs B vs ... | best (max-metric) run per accelerator |
| `sweep` | One engine across a knob | rows sorted by `op_size` |
| `regression` | Same config over time | rows sorted by `timestamp` |
| `topology` | Placement curves (same-CCD vs across-CCD, single vs SMT) | per-strategy rows, pivoted by worker count from each run's sweep |

`topology` mode reads each topology run's full sweep (not just the headline
index row) and pivots throughput by worker count; filter by `placement` and
`accelerator`/`op_size`. See [TOPOLOGY.md](TOPOLOGY.md).

## Filters

Filter tokens match `index.csv` columns exactly; multiple values are OR'd, and
multiple tokens are AND'd:

```bash
# DSA vs QAT at a fixed transfer size
compare-accel-runs.py --mode accel --filter op_size=65536 accelerator=dsa,qat

# memcpy throughput across buffer sizes
compare-accel-runs.py --mode sweep --filter accelerator=memcpy

# DSA regression for one configuration over time
compare-accel-runs.py --mode regression --filter accelerator=dsa config_hash=ab12cd34ef
```

## Metrics and baseline

- `--metric` picks the column used for the delta (`throughput_gbps`,
  `throughput_per_watt`, `latency_us_avg`, `cores_to_saturate`, ...).
- `--baseline first` uses the first selected row; pass a `run_id` to compare
  against a specific reference run.

Outputs land in `results/reports/comparison_<mode>_<ts>.{md,txt,html,csv}` and
the `.latest_comparison` marker points at them.
