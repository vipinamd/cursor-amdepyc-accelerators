#!/usr/bin/env python3
"""RAPL power sampler via turbostat on the DUT (over SSH).

start(): launches turbostat in the background on the DUT, sampling PkgWatt
and RAMWatt to a temp log. stop(): kills turbostat, fetches the log, and
averages the per-interval package/DRAM watt columns. Energy is the package
average multiplied by the measured window length.
"""
from __future__ import annotations

import re

from .base import PowerSampler, empty_power, avg, peak
from _lab_common import run_remote, run_remote_script, ssh_pass, sudo

REMOTE_LOG = "/tmp/accel_rapl.log"


class RaplSampler(PowerSampler):
    source = "rapl"

    def start(self) -> None:
        super().start()
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        interval = int(self.cfg.get("POWER_INTERVAL", "1"))
        script = f"""#!/bin/bash
{sudo(pw, "pkill -f 'turbostat' ")} 2>/dev/null || true
rm -f {REMOTE_LOG}
{sudo(pw, f"bash -c 'nohup turbostat --quiet --interval {interval} "
              f"--show PkgWatt,RAMWatt > {REMOTE_LOG} 2>&1 &'")}
sleep 1
echo RAPL_STARTED
"""
        try:
            run_remote_script(host, self.cfg["SSH_USER"], pw, script, 30)
        except Exception:  # noqa: BLE001 - sampling is best-effort
            pass

    def stop(self) -> dict:
        super().stop()
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        p = empty_power()
        p["source"] = "rapl"
        try:
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, "pkill -f 'turbostat'") + " 2>/dev/null; true", 20)
            _, out = run_remote(host, self.cfg["SSH_USER"], pw,
                                f"cat {REMOTE_LOG} 2>/dev/null || true", 20)
        except Exception:  # noqa: BLE001
            return p

        pkg, ram = self._parse(out)
        p["cpu_pkg_w_avg"] = avg(pkg)
        p["cpu_pkg_w_peak"] = peak(pkg)
        p["dram_w_avg"] = avg(ram)
        p["energy_j"] = round(p["cpu_pkg_w_avg"] * self.elapsed, 3)
        return p

    @staticmethod
    def _parse(text: str) -> tuple[list[float], list[float]]:
        """Parse turbostat PkgWatt (and RAMWatt when present) columns.

        turbostat prints a header row ('PkgWatt' / 'RAMWatt') then one summary
        row per interval. RAMWatt is absent on platforms without DRAM RAPL
        (e.g. some AMD EPYC), so the second column is optional: pkg = first
        float on the row, ram = second float when the row has two.
        """
        pkg: list[float] = []
        ram: list[float] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or "PkgWatt" in line or "RAMWatt" in line:
                continue
            if "sec" in line:  # turbostat 'N.NNNN sec' timing line
                continue
            cols = re.findall(r"[-+]?\d*\.\d+|\d+", line)
            if not cols:
                continue
            try:
                pkg.append(float(cols[0]))
                if len(cols) >= 2:
                    ram.append(float(cols[1]))
            except ValueError:
                continue
        return pkg, ram
