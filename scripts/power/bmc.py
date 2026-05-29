#!/usr/bin/env python3
"""BMC node-power sampler via IPMI DCMI (run from the DUT against its BMC).

start(): launches a background loop on the DUT polling
`ipmitool dcmi power reading` every interval into a temp log.
stop(): kills the loop, fetches the log, and averages the
"Instantaneous power reading: N Watts" values for whole-node watts.

If DCMI is unavailable the sampler degrades gracefully to zeros (the report
still shows CPU-package RAPL power when that backend is also enabled).
"""
from __future__ import annotations

import re

from .base import PowerSampler, empty_power, avg, peak
from _lab_common import run_remote, run_remote_script, ssh_pass, sudo

REMOTE_LOG = "/tmp/accel_bmc.log"


class BmcSampler(PowerSampler):
    source = "bmc"

    def start(self) -> None:
        super().start()
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        interval = int(self.cfg.get("POWER_INTERVAL", "1"))
        loop = (
            f"bash -c 'rm -f {REMOTE_LOG}; "
            f"while true; do ipmitool dcmi power reading 2>/dev/null "
            f">> {REMOTE_LOG}; sleep {interval}; done'"
        )
        script = f"""#!/bin/bash
{sudo(pw, "pkill -f 'ipmitool dcmi'")} 2>/dev/null || true
{sudo(pw, f"bash -c 'nohup {loop} >/dev/null 2>&1 &'")}
sleep 1
echo BMC_STARTED
"""
        try:
            run_remote_script(host, self.cfg["SSH_USER"], pw, script, 30)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> dict:
        super().stop()
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        p = empty_power()
        p["source"] = "bmc"
        try:
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, "pkill -f 'ipmitool dcmi'") + " 2>/dev/null; true", 20)
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, "pkill -f 'dcmi power'") + " 2>/dev/null; true", 20)
            _, out = run_remote(host, self.cfg["SSH_USER"], pw,
                                f"cat {REMOTE_LOG} 2>/dev/null || true", 20)
        except Exception:  # noqa: BLE001
            return p

        watts = [
            float(m.group(1))
            for m in re.finditer(r"Instantaneous power reading:\s*([\d.]+)\s*Watts", out)
        ]
        p["node_w_avg"] = avg(watts)
        p["node_w_peak"] = peak(watts)
        return p
