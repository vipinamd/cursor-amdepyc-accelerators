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
saturate + offload ratio), the thread sweep, profiler hotspots, a verdict, a
short table of recent runs, and recommended next steps.

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
