# Topology-aware thread placement sweep

`run --topology` explores how worker-lcore *placement* affects accelerator
throughput, not just the worker count. It is opt-in and applies to the DPDK
lcore tools (`dma_perf` in both `dma` and `cpu` modes, and `crypto_perf`); the
unpinned `memcpy_ref` baseline and unimplemented tools are skipped.

The default `run` path is unchanged - placement selection only happens with
`--topology`.

## Why placement matters on AMD EPYC

On EPYC, cores are grouped into a CCD whose compute complex (CCX) shares one L3
cache; CCDs reach memory over the I/O die. Two worker threads on SMT siblings of
one core share L1/L2; threads in one CCD share L3; threads on different CCDs share
nothing below memory. Placement therefore changes cache behaviour, cross-CCD
traffic, and achievable bandwidth. The L3 instance id from
`lscpu -e=CPU,CORE,SOCKET,NODE,L3` is the CCD grouping signal the framework uses.

## Strategies

single_core
 One physical core, one thread (count fixed at 1). The per-core baseline.

smt_pair
 One physical core plus its SMT sibling (count fixed at 2). Isolates the SMT
 gain on a single core.

same_ccd
 The worker-count sweep packed into a single L3 domain - distinct physical
 cores first, then their SMT siblings once the cores in that CCD are used up
 (so 16 in an 8-core CCD is 8 cores x 2 threads). Shows scaling while staying
 L3-local.

across_ccd
 The worker-count sweep spread one-thread-per-CCD round-robin (maximum L3
 separation), wrapping to a second core per CCD only when the count exceeds the
 CCD count. Contrast with `same_ccd` to see the L3-locality effect.

Configured per tool in [config/workloads.json](../config/workloads.json.example):

```json
"topology": {
  "strategies": ["single_core", "smt_pair", "same_ccd", "across_ccd"],
  "count_sweep": [1, 2, 4, 8, 16]
}
```

`single_core`/`smt_pair` ignore `count_sweep`; `same_ccd`/`across_ccd` use it,
clamped to the cores actually available in the target NUMA node. The worker set
is computed by `plan_lcores()` in [scripts/_topology.py](../scripts/_topology.py)
and passed to the plugin via `knobs["worker_lcores"]`; the control lcore
(`CTRL_LCORE`) is always excluded.

## Usage

```bash
python scripts/accel.py run --accel dsa --topology      # one engine, all strategies
python scripts/run-accel-benchmark.py --accel cpu_dma --topology --max-threads 8
```

This stores one run record per `(accelerator, op_size, strategy)`, each holding
the count sweep under that placement. The executive summary shows a "Topology
placement" line and an `lcores` / `CCD(s)` column in the sweep table.

## Comparing placements

```bash
python scripts/accel.py compare --mode topology
python scripts/accel.py compare --mode topology --filter accelerator=cpu_dma op_size=4096
python scripts/accel.py compare --mode topology --filter placement=same_ccd,across_ccd
```

`--mode topology` reads each run's full sweep (the index keeps only the headline
point) and pivots throughput by worker count, one row per strategy, so the
same-CCD vs across-CCD and single-core vs SMT-pair differences read off directly.

## Notes

- Read-only topology probe (one cached SSH call per host).
- Records carry the host IP and stay local (gitignored), like tuning/sanity.
- The setup-sanity gate still runs first; topology runs honour `--skip-sanity`/`--fix`.
