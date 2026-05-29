# cursor-amdepyc-accelerators

A pluggable, SSH-driven benchmarking framework for data-movement and offload
accelerators on **AMD EPYC** (SDXI, SDCI) and **Intel** (DSA, QAT, DLB)
platforms. It runs DPDK `dma-perf` / `crypto-perf` (and Linux tools), captures
**performance + power + CPU-threads-to-saturate** with `perf` / AMD uProf /
Intel VTune profiling, stores every run as a **git-tracked JSON record**, and
generates **executive summaries** and **cross-run comparison** reports.

Co-authored with Cursor. Modeled on the `brcm-ptp` lab framework.

## What it measures

For each accelerator and operation size, a run performs a **thread sweep**
(repeating the workload at 1, 2, 4, ... worker cores) and records:

- **Performance** - throughput (Gbps), ops/s, latency avg/p99
- **Power** - CPU package + DRAM watts (RAPL) and whole-node watts (BMC), energy (J)
- **CPU efficiency** - cores-to-saturate (the knee), offload/scaling ratio
- **Derived** - Gbps per watt and Gbps per core
- **Profile** - top hotspots from `perf` / uProf / VTune

## Quick start (no hardware needed)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# Run the synthetic memcpy baseline locally, then summarize + compare
python scripts/run-accel-benchmark.py --accel memcpy
python scripts/analyze-accel-run.py --latest --enrich
python scripts/compare-accel-runs.py --mode sweep --filter accelerator=memcpy
```

This exercises the full pipeline (run -> store -> analyze -> compare) on any
machine using the in-process `memcpy_ref` workload and a synthetic power model.

## Lab run (real accelerators, remote DUT)

```bash
cp config/lab.secrets.example config/lab.secrets
cp config/lab.hosts.example   config/lab.hosts
cp config/accelerators.json.example config/accelerators.json   # enable DSA/QAT/...
cp config/workloads.json.example    config/workloads.json

python scripts/accel.py discover 10.0.0.10     # write lab.hosts, verify SSH
python scripts/accel.py tune                   # check BIOS/GRUB/power/PCIe tuning
python scripts/accel.py install                # build DPDK + perf/turbostat/ipmitool
python scripts/accel.py preflight              # setup sanity (build/hugepages/bind)
python scripts/accel.py run --accel dsa,qat    # benchmark + power + profile
python scripts/accel.py report                 # executive summary
python scripts/accel.py compare --mode accel   # DSA vs QAT vs ...
python scripts/accel.py email                  # send summary (DRY_RUN by default)
```

`scripts/accel.py` uses paramiko for all SSH, so it runs natively in Windows
PowerShell and on Linux without sshpass or WSL.

## Layout

| Path | Purpose |
|------|---------|
| `scripts/accel.py` | Orchestrator: `discover, tune, install, preflight, run, report, compare, email, all` |
| `scripts/check-platform-tuning.py` | AMD BIOS/GRUB/power/PCIe tuning check (per-SoC profiles) |
| `scripts/check-setup-sanity.py` | Pre-run setup sanity (toolchain/build/hugepages/device/config) |
| `scripts/run-accel-benchmark.py` | Thread-sweep runner (power + profiler + store) |
| `scripts/analyze-accel-run.py` | Latest run -> MD/TXT/HTML executive summary |
| `scripts/compare-accel-runs.py` | Cross-run comparison (accel / sweep / regression) |
| `scripts/plugins/` | Workload runners (memcpy_ref, dma_perf, crypto_perf, eventdev) |
| `scripts/power/` | RAPL / BMC / synthetic power samplers |
| `scripts/profilers/` | perf / uProf / VTune backends |
| `config/` | `lab.secrets`, `lab.hosts`, `accelerators.json`, `workloads.json` (examples committed) |
| `results/runs/` | Per-run JSON records (git-tracked) |
| `results/index.csv` | Flat index of all runs (git-tracked, drives comparison) |
| `results/reports/` | Executive summaries and comparison reports |
| `docs/` | Framework, per-accelerator, profiling, power, reporting docs |

## Documentation

- [docs/FRAMEWORK.md](docs/FRAMEWORK.md) - architecture, run record schema, results store
- [docs/TUNING.md](docs/TUNING.md) - AMD BIOS/GRUB/power/PCIe tuning check (per-SoC profiles)
- [docs/SANITY.md](docs/SANITY.md) - setup sanity preflight + `--fix` self-heal
- [docs/ACCELERATORS.md](docs/ACCELERATORS.md) - DSA, QAT, DLB, SDXI, SDCI setup + devargs
- [docs/PROFILING.md](docs/PROFILING.md) - perf, AMD uProf, Intel VTune
- [docs/POWER.md](docs/POWER.md) - RAPL (turbostat) and BMC (IPMI DCMI)
- [docs/REPORTING.md](docs/REPORTING.md) - summaries and email
- [docs/COMPARISON.md](docs/COMPARISON.md) - comparison modes and filters
- [docs/INSTALL.md](docs/INSTALL.md) - prerequisites and DPDK build
- [docs/RUNBOOK.md](docs/RUNBOOK.md) - step-by-step procedures
