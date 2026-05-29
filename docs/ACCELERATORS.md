# Accelerators

Each engine is described in `config/accelerators.json` and bound to a runner
plugin (`tool`). Enable an engine with `"enabled": true` and set its PCI BDF
and any DPDK devargs.

| Accelerator | Vendor | Tool (plugin) | DPDK driver | Status |
|-------------|--------|---------------|-------------|--------|
| memcpy | reference | `memcpy_ref` | none (local) | implemented |
| DSA | Intel | `dma_perf` | `dmadev/idxd` | implemented |
| QAT | Intel | `crypto_perf` | `crypto/qat` | implemented |
| DLB | Intel | `eventdev` | `event/dlb2` | scaffolded |
| SDXI | AMD | `dma_perf` | `dmadev/sdxi` | scaffolded |
| SDCI | AMD | `dma_perf` | (platform) | scaffolded |

## Intel DSA (Data Streaming Accelerator)

- Kernel: `idxd` driver; configure work queues with `accel-config`.
- DPDK: `dmadev` (idxd). Bind the device to `vfio-pci` or use the kernel
  work-queue char device per the DPDK dmadev idxd guide.
- Run: `dma_perf` generates a `dpdk-test-dma-perf` INI (`DMA_MEM_COPY`) with
  one DMA channel per worker lcore in the sweep.

## Intel QAT (QuickAssist)

- Kernel: `qat_4xxx`; enable VFs via sysfs, bind VFs to `vfio-pci`.
- DPDK: `crypto/qat`. Set `bdf` to a QAT VF.
- Run: `crypto_perf` runs `dpdk-test-crypto-perf --ptest throughput` with the
  cipher/auth algorithms from `workloads.json`.

## Intel DLB (Dynamic Load Balancer) - scaffolded

- Kernel: `dlb2`; bind PF/VF to `vfio-pci`.
- DPDK: `event/dlb2` via `dpdk-test-eventdev`. The plugin is wired but marked
  `implemented=False` until validated; run it with `--force-stubs`.

## AMD SDXI (Smart Data Accelerator Interface) - scaffolded

- DMA-class offload exposed through DPDK `dmadev`. Set the device BDF/devargs
  in `accelerators.json` and use the `dma_perf` tool. Platform enablement
  varies by EPYC generation; confirm the dmadev driver name on your build.

## AMD SDCI (Smart Data Cache Injection) - scaffolded

- Cache-injection path; benchmarked via the DMA runner where exposed through
  `dmadev`, or via a Linux application. Fill in the BDF/setup once the
  platform exposes it.

## Device binding tips

- Reserve hugepages and bind to `vfio-pci` before running (the runner's setup
  is intentionally minimal; do persistent binding via `dpdk-devbind.py` or
  systemd on the DUT).
- Pin worker lcores to the NUMA node local to the device (`CTRL_LCORE` and the
  sweep base in `lab.hosts`).
