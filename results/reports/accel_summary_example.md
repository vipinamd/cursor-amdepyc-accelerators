# Accelerator benchmark summary (template)

- **Generated (UTC):** 2026-05-29T04:23:03.865856+00:00
- **Verdict:** TEMPLATE

This is a TEMPLATE summary illustrating the report the framework emits after a run. Metric columns show <measured> placeholders; a real run fills them from DPDK dma-perf / crypto-perf plus RAPL/BMC power and the CPU-threads-to-saturate sweep.

## Run context

| Item | Value |
|---|---|
| Report type | TEMPLATE (placeholders, no lab data) |
| Framework | cursor-amdepyc-accelerators |
| DUT | <dut-host> |
| CPU / SoC | <cpu-model> / <soc> |
| Power source | RAPL (CPU pkg/DRAM) + BMC (node) |
| Profiler | perf / AMD uProf / Intel VTune |
| Generated (UTC) | 2026-05-29 04:23 UTC |

## Accelerator comparison (placeholders)

| Accelerator | Vendor | Tool | DPDK driver | Status | Throughput (Gbps) | Pkg W | Gbps/W | Cores-to-sat | Offload ratio |
|---|---|---|---|---|---|---|---|---|---|
| dsa | intel | dma_perf | dmadev/idxd | implemented | <measured> | <measured> | <measured> | <measured> | <measured> |
| qat | intel | crypto_perf | crypto/qat | implemented | <measured> | <measured> | <measured> | <measured> | <measured> |
| dlb | intel | eventdev | event/dlb2 | scaffolded | <measured> | <measured> | <measured> | <measured> | <measured> |
| sdxi | amd | dma_perf | dmadev/sdxi | scaffolded | <measured> | <measured> | <measured> | <measured> | <measured> |
| sdci | amd | dma_perf |  | scaffolded | <measured> | <measured> | <measured> | <measured> | <measured> |
| memcpy | reference | memcpy_ref | n/a | implemented | <measured> | <measured> | <measured> | <measured> | <measured> |

## Metrics captured per run

- Performance: throughput (Gbps), ops/s, latency avg/p99
- Power: CPU package + DRAM watts (RAPL), node watts (BMC), energy (J)
- CPU efficiency: cores-to-saturate (knee), offload/scaling ratio
- Derived: Gbps/W and Gbps/core
- Profile: top hotspots (perf / uProf / VTune)

## Comparison modes

- accel: A vs B (e.g. DSA vs QAT vs SDXI)
- sweep: one engine across op size / threads
- regression: same config over time (by commit/date)

