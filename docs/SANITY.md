# Setup sanity preflight

`scripts/check-setup-sanity.py` (phase `accel.py preflight`) verifies that a DUT
is actually ready to run a benchmark *before* the sweep starts: the compiler
toolchain, the DPDK build, free hugepages, the device binding, the runtime
configuration, and (rolled up from the platform tuning snapshot) BIOS/GRUB.

The same checks run automatically inside `run-accel-benchmark.py` as a per
accelerator gate, so a misconfigured DUT fails fast with a clear reason instead
of producing an empty or misleading run.

The check is **read-only and hard-gating by default**. All remediation
(installing packages, building DPDK, editing GRUB, rebooting, reserving
hugepages, binding the device) happens only with `--fix`. BIOS is never changed
automatically -- it is reported for manual remediation.

## What it checks

Each item is classified PASS / WARN / FAIL and carries a `blocker` flag. A
blocker means the benchmark cannot run and the accelerator is skipped (unless
`--fix` clears it).

COMPILER (`gcc`, `meson`, `ninja`, `pkg-config`)
 WARN if missing -- needed only to build DPDK, fixable with `--fix`.

BUILD (the per-tool DPDK app, e.g. `dpdk-test-dma-perf`)
 FAIL + blocker if the binary is missing. In-process tools (`memcpy_ref`)
 need no DPDK build.

HUGEPAGES (free `1G`/`2M` pages, per `HUGEPAGE_MODE`)
 FAIL + blocker if fewer than `min_free_hugepages` are free. Skipped for the
 in-process reference tool.

DEVICE (the accelerator `bdf` driver)
 FAIL + blocker if not bound to `vfio_driver` (default `vfio-pci`). Skipped for
 CPU/in-memory modes that take no `bdf`.

CONFIG (tool implemented; `bdf` present for hardware DMA mode)
 FAIL + blocker on a scaffolded tool or a missing `bdf`.

GRUB (rolled up from the tuning snapshot)
 Blocker in hardware DMA mode when required kernel params (e.g. `amd_iommu=on`,
 `iommu=pt`) are missing, since vfio needs the IOMMU; WARN otherwise.

BIOS (rolled up from the tuning snapshot)
 WARN, report-only -- set in firmware/BMC (see `accel.py tune`).

TOOLS (`turbostat`, `ipmitool`, `perf`)
 WARN if missing -- they only affect power sampling / profiling.

Thresholds live in [config/sanity.json](../config/sanity.json.example)
(`min_free_hugepages`, `vfio_driver`); copy the `.example` to enable overrides.

## The `--fix` loop

With `--fix`, blockers (and other fixable WARNs) trigger remediation that reuses
the brcm-ptp configurator pattern and the Step 1 reboot helpers:

1. `install_toolchain` -- `apt-get install` the toolchain + measurement tools
   (same package list as `accel.py install`).
2. `build_dpdk` -- fetch/extract/`meson`/`ninja` the configured DPDK.
3. `reserve_hugepages` -- write `nr_hugepages` for the configured page size.
4. `apply_grub` -- rewrite `GRUB_CMDLINE_LINUX` (backing up `/etc/default/grub`
   first), regenerate GRUB, then **reboot + wait for SSH**.
5. `bind_vfio` -- `modprobe vfio-pci` + `dpdk-devbind.py --bind=vfio-pci <bdf>`.

After remediation (and a reboot if GRUB changed) the checks re-run. If a blocker
remains, the accelerator is still skipped.

## Usage

```bash
# Read-only preflight for the first enabled accelerator
python scripts/accel.py preflight

# A specific accelerator, with remediation + reboot if needed
python scripts/accel.py preflight --accel dsa --fix

# As part of a run: gate every accelerator, remediate blockers
python scripts/accel.py run --fix
python scripts/run-accel-benchmark.py --skip-sanity   # bypass the gate
```

Reports are written to `results/reports/sanity_*.{md,txt,html}` and the JSON
record to `results/sanity/`. Like the tuning artifacts these carry the DUT IP
and are gitignored. The per-run snapshot is also embedded in each run record
(`record["setup"]`) and rendered as a "Setup sanity" table in the executive
summary.
