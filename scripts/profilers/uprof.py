#!/usr/bin/env python3
"""AMD uProf profiler (AMDuProfCLI) on the DUT over SSH. WIRED.

uProf is license/installer-gated, so this backend detects AMDuProfCLI and,
when present, runs a time-based CPU profile collection for the benchmark
window, then fetches the report. When absent it degrades to an empty profile
and the runner falls back to perf. See docs/PROFILING.md for install steps.
"""
from __future__ import annotations

from pathlib import Path

from .base import Profiler, empty_profile
from _lab_common import run_remote, run_remote_script, get_remote, ssh_pass, sudo

REMOTE_DIR = "/tmp/accel_uprof"


class UprofProfiler(Profiler):
    name = "uprof"

    def available(self) -> bool:
        host = self.cfg["DUT_HOST"]
        try:
            _, out = run_remote(host, self.cfg["SSH_USER"], ssh_pass(self.cfg, host),
                                "command -v AMDuProfCLI 2>/dev/null || true", 20)
            return "AMDuProfCLI" in out
        except Exception:  # noqa: BLE001
            return False

    def start(self, bundle: Path) -> None:
        super().start(bundle)
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        script = f"""#!/bin/bash
rm -rf {REMOTE_DIR}; mkdir -p {REMOTE_DIR}
{sudo(pw, "pkill -f AMDuProfCLI")} 2>/dev/null || true
# Time-based profile, system-wide, finalized on SIGINT at stop().
{sudo(pw, f"bash -c 'nohup AMDuProfCLI collect --config tbp -a "
              f"-o {REMOTE_DIR}/run >/tmp/accel_uprof.log 2>&1 &'")}
sleep 1
echo UPROF_STARTED
"""
        try:
            run_remote_script(host, self.cfg["SSH_USER"], pw, script, 30)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> dict:
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        info = empty_profile()
        info["profiler"] = "uprof"
        try:
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, "pkill -INT -f AMDuProfCLI") + " 2>/dev/null; true", 20)
            run_remote(host, self.cfg["SSH_USER"], pw, "sleep 3", 10)
            # Produce a CSV summary report from the collected database.
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, f"AMDuProfCLI report -i {REMOTE_DIR} "
                                f"-o {REMOTE_DIR}/report 2>/dev/null") + "; true", 120)
            _, log = run_remote(host, self.cfg["SSH_USER"], pw,
                                "cat /tmp/accel_uprof.log 2>/dev/null || true", 20)
        except Exception:  # noqa: BLE001
            return info
        if self.bundle is not None and log.strip():
            local = self.bundle / "uprof.log"
            local.write_text(log, encoding="utf-8")
            info["artifacts"].append(local.name)
            # Best-effort: pull a CSV summary if uProf produced one.
            csv_remote = f"{REMOTE_DIR}/report.csv"
            csv_local = self.bundle / "uprof_report.csv"
            if get_remote(host, self.cfg["SSH_USER"], pw, csv_remote, csv_local):
                info["artifacts"].append(csv_local.name)
        return info
