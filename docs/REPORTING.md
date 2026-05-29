# Reporting and email

## Executive summary

```bash
python scripts/analyze-accel-run.py --latest --enrich
# or a specific run:
python scripts/analyze-accel-run.py --run results/runs/dsa/dsa_dma_perf_<ts>.json
```

Outputs three files under `results/reports/`:

| File | Use |
|------|-----|
| `accel_summary_<ts>.md` | human-readable, git-friendly |
| `accel_summary_<ts>.txt` | plain-text email body with ASCII tables |
| `accel_summary_<ts>.html` | HTML tables for email clients |

The summary covers overview, performance, power, CPU efficiency (cores to
saturate + offload ratio), a **Setup sanity** table, a **Platform tuning vs AMD
guide** table, the thread sweep, profiler hotspots, a verdict, a short table of
recent runs, and recommended next steps.

### Setup sanity

For remote runs the runner captures a setup-sanity snapshot before the sweep
(see [SANITY.md](SANITY.md)) and stores it in `record["setup"]`. The summary
renders it as a `Category | Item | Expected | Observed | Status` table with a
one-line verdict, e.g. `Setup: FAIL - 2 blocker(s), 3 issue(s)`. Non-PASS rows
are highlighted and hard blockers are tagged `(blocker)`; when `--fix` ran, an
"Applied remediations" list is included and a next-step points to
`accel.py preflight --fix`. Local runs show `not captured (local run)`.

### Topology placement

When a run is produced by `run --topology` (see [TOPOLOGY.md](TOPOLOGY.md)), the
record carries a `placement` block and the summary adds a "Topology placement"
line to the Overview (strategy, NUMA node, L3 domains, SMT) and an
`lcores` / `CCD(s)` column to the thread-sweep table. Cross-placement curves
(same-CCD vs across-CCD, single vs SMT) are produced by
`compare --mode topology`, which expands each run's sweep and pivots throughput
by worker count.

### Platform tuning vs AMD guide

For remote runs, the runner captures a tuning snapshot (GRUB / BIOS-observable /
power / system config) once per host via [TUNING.md](TUNING.md) and stores it in
the run record. The summary renders it as a `Category | Item | Expected |
Observed | Status` table and a one-line verdict, e.g.
`Tuning: WARN - 3 difference(s) from guide`. Differences (WARN/FAIL) are
highlighted (bold/colored in HTML); when there are any, a next-step points to
`accel.py tune`. Local/synthetic runs show `not captured (local run)`.

## Email

```bash
cp config/report-email.example config/report-email.conf
# edit TO/FROM and the SMTP relay; set DRY_RUN=0 to actually send
python scripts/send-report-smtp.py        # sends the latest summary
# or, via the orchestrator (analyze + send):
python scripts/accel.py email
```

`report-email.conf` defaults to `DRY_RUN=1`, which prints the message instead
of sending. The email uses the TXT body with the HTML summary as an
alternative part so it renders nicely in clients.
