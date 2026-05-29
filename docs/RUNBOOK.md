# Runbook

Step-by-step procedures. See [INSTALL.md](INSTALL.md) for prerequisites.

## 0. Dry-run on any machine (no hardware)

```bash
python scripts/run-accel-benchmark.py --accel memcpy
python scripts/analyze-accel-run.py --latest --enrich
python scripts/compare-accel-runs.py --mode sweep --filter accelerator=memcpy
```

Confirms the pipeline: run -> `results/runs/*.json` + `results/index.csv` ->
summary -> comparison.

## 1. Bring up a DUT

```bash
cp config/lab.secrets.example config/lab.secrets   # edit creds
python scripts/accel.py discover 10.0.0.10         # writes config/lab.hosts
python scripts/accel.py install                    # builds DPDK + tools
```

## 1a. Check platform tuning

```bash
python scripts/accel.py tune                       # BIOS/GRUB/power/PCIe (read-only)
python scripts/accel.py tune --bios-redfish        # also read actual BIOS via BMC
python scripts/accel.py tune --apply-grub --reboot # apply recommended GRUB + reboot
```

See [TUNING.md](TUNING.md) for the per-SoC profiles and the manual BIOS
checklist.

## 2. Enable accelerators

Edit `config/accelerators.json`: set `"enabled": true` and the `bdf`/`devargs`
for each engine (DSA, QAT, ...). Bind devices to `vfio-pci` on the DUT.

## 2a. Setup sanity preflight

```bash
python scripts/accel.py preflight                  # toolchain/build/hugepages/bind/config
python scripts/accel.py preflight --accel dsa      # one engine
python scripts/accel.py preflight --accel dsa --fix # remediate blockers (+ reboot if GRUB)
```

The same checks run automatically before each `run` and skip any accelerator
with an unresolved blocker. See [SANITY.md](SANITY.md).

## 3. Run benchmarks

```bash
python scripts/accel.py run --accel dsa            # one engine
python scripts/accel.py run --accel dsa,qat        # several
python scripts/accel.py run                        # all enabled
python scripts/accel.py run --fix                  # remediate setup blockers, then run
python scripts/accel.py run --skip-sanity          # bypass the preflight gate
python scripts/accel.py run --accel dlb --force-stubs   # scaffolded engines
```

Each run writes a per-run JSON, appends `index.csv`, and saves a bundle with
raw logs and profiler artifacts.

## 4. Summarize and compare

```bash
python scripts/accel.py report                     # latest run summary
python scripts/accel.py compare --mode accel --filter op_size=65536
python scripts/accel.py compare --mode regression --filter accelerator=dsa
```

## 5. Email (optional)

```bash
cp config/report-email.example config/report-email.conf   # edit TO/FROM, DRY_RUN=0
python scripts/accel.py email
```

## 6. Commit results

The per-run JSON records and `index.csv` are git-tracked. Commit them after a
session so the runs are retrievable for future comparison:

```bash
git add results/runs results/index.csv results/reports
git commit -m "results: <engine> run on <date>"
```

(Bundles and `.latest_*` markers are gitignored.)

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Throughput 0 | device bound to vfio-pci? hugepages reserved? correct `bdf`/`devargs`? |
| Power all zero | turbostat/ipmitool installed? `POWER_SOURCE` set? run as root? |
| No hotspots | profiler installed on DUT? `perf_event_paranoid`? |
| Engine never saturates | raise `thread_sweep` / `MAX_WORKER_LCORES` |
| SSH fails | `accel.py discover` reports the paramiko error; verify creds in `lab.secrets` |
