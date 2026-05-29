#!/usr/bin/env python3
"""Linux perf profiler (system-wide) on the DUT over SSH.

start(): background `perf record -a -g` to /tmp/accel_perf.data.
stop():  SIGINT perf to finalize, run `perf report --stdio` for the top
         symbols, fetch perf.data into the bundle, and parse hotspots.
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import Profiler, empty_profile
from _lab_common import run_remote, run_remote_script, get_remote, ssh_pass, sudo

REMOTE_DATA = "/tmp/accel_perf.data"
REMOTE_REPORT = "/tmp/accel_perf_report.txt"


class PerfProfiler(Profiler):
    name = "perf"

    def available(self) -> bool:
        host = self.cfg["DUT_HOST"]
        try:
            code, out = run_remote(host, self.cfg["SSH_USER"], ssh_pass(self.cfg, host),
                                   "command -v perf 2>/dev/null || true", 20)
            return "perf" in out
        except Exception:  # noqa: BLE001
            return False

    def start(self, bundle: Path) -> None:
        super().start(bundle)
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        freq = int(self.cfg.get("PROFILER_FREQ", "997"))
        script = f"""#!/bin/bash
{sudo(pw, "pkill -INT -f 'perf record'")} 2>/dev/null || true
rm -f {REMOTE_DATA}
{sudo(pw, f"bash -c 'nohup perf record -a -g -F {freq} -o {REMOTE_DATA} "
              f">/tmp/accel_perf_rec.log 2>&1 &'")}
sleep 1
echo PERF_STARTED
"""
        try:
            run_remote_script(host, self.cfg["SSH_USER"], pw, script, 30)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> dict:
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        info = empty_profile()
        info["profiler"] = "perf"
        try:
            # SIGINT lets perf flush the data file cleanly.
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, "pkill -INT -f 'perf record'") + " 2>/dev/null; true", 20)
            run_remote(host, self.cfg["SSH_USER"], pw, "sleep 2", 10)
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, f"perf report --stdio -i {REMOTE_DATA} > {REMOTE_REPORT} 2>/dev/null")
                       + "; true", 120)
            _, rpt = run_remote(host, self.cfg["SSH_USER"], pw,
                                f"cat {REMOTE_REPORT} 2>/dev/null || true", 30)
        except Exception:  # noqa: BLE001
            return info

        info["hotspots"] = self._parse_hotspots(rpt)
        if self.bundle is not None:
            local = self.bundle / "perf_report.txt"
            local.write_text(rpt, encoding="utf-8")
            info["artifacts"].append(local.name)
            data_local = self.bundle / "perf.data"
            if get_remote(host, self.cfg["SSH_USER"], pw, REMOTE_DATA, data_local):
                info["artifacts"].append(data_local.name)
        return info

    @staticmethod
    def _parse_hotspots(report: str, top: int = 10) -> list[dict]:
        hot: list[dict] = []
        for line in report.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # perf report rows: "  42.13%  comm  dso  [.] symbol"
            m = re.match(r"([\d.]+)%\s+\S+\s+\S+\s+\[[^\]]*\]\s+(.+)", line)
            if m:
                hot.append({"symbol": m.group(2).strip(), "pct": float(m.group(1))})
            if len(hot) >= top:
                break
        return hot
