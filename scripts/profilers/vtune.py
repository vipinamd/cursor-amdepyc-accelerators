#!/usr/bin/env python3
"""Intel VTune profiler (vtune CLI) on the DUT over SSH. WIRED.

VTune is installer/license-gated, so this backend detects the `vtune` CLI
and, when present, runs a hotspots collection for the benchmark window and
fetches the summary. When absent it degrades to an empty profile and the
runner falls back to perf. See docs/PROFILING.md for install steps.
"""
from __future__ import annotations

from pathlib import Path

from .base import Profiler, empty_profile
from _lab_common import run_remote, run_remote_script, get_remote, ssh_pass, sudo

REMOTE_DIR = "/tmp/accel_vtune"


class VtuneProfiler(Profiler):
    name = "vtune"

    def available(self) -> bool:
        host = self.cfg["DUT_HOST"]
        try:
            _, out = run_remote(host, self.cfg["SSH_USER"], ssh_pass(self.cfg, host),
                                "command -v vtune 2>/dev/null || true", 20)
            return "vtune" in out
        except Exception:  # noqa: BLE001
            return False

    def start(self, bundle: Path) -> None:
        super().start(bundle)
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        script = f"""#!/bin/bash
rm -rf {REMOTE_DIR}
{sudo(pw, "pkill -f 'vtune'")} 2>/dev/null || true
{sudo(pw, f"bash -c 'nohup vtune -collect hotspots -r {REMOTE_DIR} "
              f"-- sleep 100000 >/tmp/accel_vtune.log 2>&1 &'")}
sleep 1
echo VTUNE_STARTED
"""
        try:
            run_remote_script(host, self.cfg["SSH_USER"], pw, script, 30)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> dict:
        host = self.cfg["DUT_HOST"]
        pw = ssh_pass(self.cfg, host)
        info = empty_profile()
        info["profiler"] = "vtune"
        try:
            run_remote(host, self.cfg["SSH_USER"], pw,
                       sudo(pw, "pkill -INT -f 'vtune'") + " 2>/dev/null; true", 20)
            run_remote(host, self.cfg["SSH_USER"], pw, "sleep 3", 10)
            _, summary = run_remote(host, self.cfg["SSH_USER"], pw,
                                    sudo(pw, f"vtune -report hotspots -r {REMOTE_DIR} 2>/dev/null")
                                    + " || true", 120)
        except Exception:  # noqa: BLE001
            return info
        if self.bundle is not None and summary.strip():
            local = self.bundle / "vtune_hotspots.txt"
            local.write_text(summary, encoding="utf-8")
            info["artifacts"].append(local.name)
        return info
