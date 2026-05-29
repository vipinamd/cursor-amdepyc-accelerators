# AMD platform tuning check

`scripts/check-platform-tuning.py` (phase `accel.py tune`) verifies that a DUT
meets the DPDK AMD platform recommendations before benchmarking. It is based on
[How to get best performance on AMD platform](https://doc.dpdk.org/guides/linux_gsg/amd_platform.html)
and the AMD EPYC tuning guides it links.

The check is **read-only by default**. GRUB edits and reboots only happen with
explicit flags.

## What it checks

The DUT is probed over SSH and each item is classified PASS / WARN / FAIL
against the per-family profile in [config/amd-tuning.json](../config/amd-tuning.json.example):

GRUB kernel parameters (substring match on `/proc/cmdline`)
 `amd_iommu=on`, `iommu=pt`, `hugepagesz=1G`, `pcie_aspm=off`,
 `processor.max_cstate=0`.

BIOS (OS-observable indicators)
 SMT (`lscpu` threads/core), NPS (NUMA nodes / sockets), max C-state,
 core boost, IOMMU, x2APIC, and the cpufreq governor.

Power
 `amd_pstate` driver mode (`passive` is required for `rte_power` on EPYC,
 23.11+) and the `amd_hsmp` module (needed for `rte_power_uncore`, 25.03+).

System
 Kernel version (info), transparent hugepages (expect `never`), reserved 1G
 hugepages (expect `>=1`), and the active `tuned-adm` profile (recommend
 `accelerator-performance` / `throughput-performance`).

PCIe
 Link speed and width (`lspci -vv` `LnkSta`) for accelerator/NIC BDFs,
 compared to the family's minimum (Gen4 x16 / Gen5 x16).

BIOS (not OS-observable)
 A manual checklist (Determinism = Performance, Global/DF C-states disabled,
 APBDIS / fixed SOC P-state, Core Boost, NPS, TSME off, Preferred IO, etc.)
 is printed in the report since these can only be set in firmware or via the
 BMC.

## SoC families

The family is detected from the `lscpu` model (or `CPU_SOC` in `lab.hosts`) via
`cpu_model_map`. Profiles ship for: rome, milan, genoa, bergamo, siena, turin.
Edit `config/amd-tuning.json` (copy from the `.example`) to adjust expected
values for your workload, e.g. NPS1 vs NPS4 or SMT on vs off.

## Usage

```bash
# Read-only check + report (MD/TXT/HTML under results/reports/tuning_*)
python scripts/accel.py tune

# Also check actual BIOS settings via the BMC Redfish API (needs DUT_BMC_IP)
python scripts/accel.py tune --bios-redfish

# Link-check specific PCIe devices
python scripts/accel.py tune --pcie 41:00.0,42:00.0

# Apply the recommended GRUB line, then reboot and re-check
python scripts/accel.py tune --apply-grub --reboot
```

`install` and `run` also print a non-blocking one-line tuning verdict so a
misconfigured box is visible without failing the phase.

## Captured per run and shown in the summary

For remote runs, `run-accel-benchmark.py` captures a host-level tuning snapshot
once (cached per host) using the same probe and stores it in the run record
under `tuning` (family, verdict, and the checks vs the guide). The executive
summary ([REPORTING.md](REPORTING.md)) renders this as a "Platform tuning vs AMD
guide" table with the differences highlighted, so every report shows the tuning
state that was in effect during that run. Local/synthetic runs skip capture and
the summary shows `not captured (local run)`. The per-run snapshot is
host-level (no PCIe link check); use `accel.py tune --pcie ...` for device link
verification.

## GRUB apply

`--apply-grub` backs up `/etc/default/grub` to `/etc/default/grub.bak.<ts>`,
rewrites `GRUB_CMDLINE_LINUX` from the family `apply_base`, regenerates GRUB
(`update-grub`, falling back to `grub-mkconfig`), and reports whether a reboot
is needed. The line is built from:

- `TUNE_HUGEPAGES` (1G hugepages to reserve)
- `TUNE_ISOLCPUS` (isolated worker-core range; leave empty to skip
  `isolcpus`/`nohz_full`/`rcu_nocbs`)

both set in `config/lab.hosts`. Always review the proposed line; core isolation
must match your actual topology.

## BIOS via Redfish

`--bios-redfish` reads `/redfish/v1/Systems/*/Bios` `Attributes` from the BMC at
`DUT_BMC_IP` (using `BMC_USER` / `BMC_PASS`) and matches them against the
`common.bios_redfish` map. Attribute names are vendor-specific, so matching is
case-insensitive/substring; unmatched attributes are reported as INFO. The check
is skipped cleanly when `DUT_BMC_IP` is unset or the BMC has no Redfish BIOS
resource.

## Output

- `results/tuning/<host>_<ts>.json` - full record (observed values + checks)
- `results/reports/tuning_<ts>.{md,txt,html}` - the report

Tuning records and reports contain the lab host IP, so they are gitignored
(local only), consistent with the rest of the results store.

## PCIe link check (manual)

```bash
lspci -s 41:00.0 -vv | grep LnkSta
# LnkSta: Speed 32GT/s, Width x16, ...
```

Gen5 is 32 GT/s, Gen4 is 16 GT/s; use a x16 slot with adequate bandwidth and
proximity to the target NUMA node.
