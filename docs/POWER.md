# Power measurement

Set `POWER_SOURCE` in `config/lab.hosts` to `rapl`, `bmc`, `both`,
`synthetic`, or `none`. `POWER_INTERVAL` controls the sampling period
(seconds). The sampler runs in the background on the DUT for the whole
benchmark window; on `stop()` it averages the samples and computes energy.

Local (non-remote) workloads such as `memcpy_ref` always use the **synthetic**
sampler because there is no DUT to read sensors from. Synthetic values are
clearly labelled `source: synthetic` and flagged in every report so they are
never mistaken for measured power.

## RAPL (CPU package + DRAM) via turbostat

```bash
sudo apt-get install linux-tools-common linux-tools-$(uname -r)
sudo turbostat --quiet --interval 1 --show PkgWatt,RAMWatt
```

The backend launches turbostat in the background and parses the `PkgWatt` and
`RAMWatt` columns into `cpu_pkg_w_avg/peak` and `dram_w_avg`. Energy (J) is the
package average times the measured window length.

## BMC node power via IPMI DCMI

```bash
sudo apt-get install ipmitool
ipmitool dcmi power reading
# -> "Instantaneous power reading:  NNN Watts"
```

The backend polls `ipmitool dcmi power reading` on the DUT against its local
BMC and averages the instantaneous readings into `node_w_avg/peak`. If DCMI is
unavailable it degrades to zero node power (RAPL still provides CPU watts when
`both` is selected).

## Derived efficiency

`throughput_per_watt` uses CPU package watts when available (the
offload-efficiency view), falling back to node watts. `throughput_per_core`
uses the worker-core count at the saturating data point. Both appear in the
run record, the executive summary, and the comparison reports.
