# Install

## Orchestrator (Windows or Linux)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt     # paramiko
```

Only `paramiko` is required. The synthetic `memcpy` baseline runs locally with
no further setup, so you can validate the whole pipeline immediately.

## DUT prerequisites (for real accelerator runs)

`python scripts/accel.py install` connects to the DUT over SSH and installs
the build + measurement tools, then builds DPDK:

- build: `meson`, `ninja-build`, `build-essential`, `python3-pyelftools`,
  `libnuma-dev`, `libssl-dev`, `pkg-config`, `curl`
- measurement: `linux-tools-*` (perf, turbostat), `ipmitool`
- DPDK: downloaded from `DPDK_URL` and built; this produces
  `dpdk-test-dma-perf` and `dpdk-test-crypto-perf` in `build/app/`

Profilers (AMD uProf, Intel VTune) are license/installer gated and must be
installed manually on the DUT - see [PROFILING.md](PROFILING.md).

## Configuration files

```bash
cp config/lab.secrets.example       config/lab.secrets       # SSH + BMC creds
cp config/lab.hosts.example         config/lab.hosts         # DUT IP, DPDK paths, power/profiler
cp config/accelerators.json.example config/accelerators.json # enable engines, set BDFs
cp config/workloads.json.example    config/workloads.json    # sizes, duration, thread sweep
cp config/report-email.example      config/report-email.conf # optional, for email
```

Real config files are gitignored; only the `.example` files are committed.

## DUT hardware setup

- Reserve hugepages and bind accelerator devices to `vfio-pci`
  (`dpdk-devbind.py`), or configure kernel work queues (DSA via `accel-config`).
- Pin worker lcores to the device-local NUMA node (`CTRL_LCORE`,
  `MAX_WORKER_LCORES` in `lab.hosts`).
