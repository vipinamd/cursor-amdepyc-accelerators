# Profiling

Set `PROFILER` in `config/lab.hosts` to `perf`, `uprof`, `vtune`, or `none`.
The profiler samples the DUT for the whole benchmark window; on `stop()` it
fetches artifacts into the run bundle and parses the top hotspots into the run
record (`profile.hotspots`). If the requested profiler is not installed, the
framework falls back to `perf`, then to no profiling, so a run never fails
because a profiler is missing.

## Linux perf (default, implemented)

```bash
sudo apt-get install linux-tools-common linux-tools-$(uname -r)
# allow non-root sampling if desired:
echo -1 | sudo tee /proc/sys/kernel/perf_event_paranoid
```

The backend runs `perf record -a -g -F <PROFILER_FREQ>` in the background and
finalizes with SIGINT, then `perf report --stdio` for hotspots. `perf.data`
and `perf_report.txt` are saved into the bundle.

## AMD uProf (wired)

Install AMD uProf and ensure `AMDuProfCLI` is on PATH. The backend runs a
time-based profile (`AMDuProfCLI collect --config tbp -a`) and produces a CSV
report. Download: AMD developer site (license/installer gated).

```bash
AMDuProfCLI collect --config tbp -a -o /tmp/run -- sleep 30
AMDuProfCLI report -i /tmp/run -o /tmp/run/report
```

## Intel VTune (wired)

Install Intel VTune Profiler and source its environment so `vtune` is on PATH.
The backend runs `vtune -collect hotspots` and reports the summary.

```bash
source /opt/intel/oneapi/vtune/latest/env/vars.sh
vtune -collect hotspots -r /tmp/r -- sleep 30
vtune -report hotspots -r /tmp/r
```

## Notes

- Profiling requires root (or appropriate capabilities) on the DUT.
- Profiler artifacts can be large; the bundle directory is gitignored. The
  parsed hotspot summary lives inside the per-run JSON, which is committed.
