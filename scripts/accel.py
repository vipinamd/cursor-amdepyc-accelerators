#!/usr/bin/env python3
"""Accelerator benchmark cross-platform orchestrator (Windows + Linux).

All remote access goes through paramiko (see _lab_common.py), so no sshpass
or WSL is required.

Phases:
  discover <DUT_IP> [PKTGEN_IP]  Write config/lab.hosts, verify SSH
  install                        Build DPDK + ensure perf/turbostat/ipmitool
  run [--accel ...]              Run benchmarks (delegates to run-accel-benchmark.py)
  report                         Analyze the latest run -> executive summary
  compare [args...]              Cross-run comparison (delegates to compare-accel-runs.py)
  email                          Analyze + send summary via SMTP
  all <DUT_IP>                   discover -> install -> run -> report
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _lab_common as lab
from _lab_common import (
    CONFIG, expand_home, load_config, log, run_remote, run_remote_script,
    ssh_pass, sudo, verify_ssh,
)


def _py() -> str:
    return sys.executable or "python3"


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------
def cmd_discover(cfg: dict, args: argparse.Namespace) -> int:
    if not args.hosts:
        log("usage: accel.py discover <DUT_IP> [PKTGEN_IP]")
        return 1
    dut = args.hosts[0]
    pktgen = args.hosts[1] if len(args.hosts) > 1 else ""
    out = CONFIG / "lab.hosts"

    preserved: list[str] = []
    core_keys = {"DUT_HOST", "PKTGEN_HOST"}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.split("=", 1)[0].strip() in core_keys:
                continue
            preserved.append(line.rstrip())

    lines = [
        f"# Auto-generated {datetime.now(timezone.utc).isoformat()}",
        f"DUT_HOST={dut}",
        f"PKTGEN_HOST={pktgen}",
    ]
    if preserved:
        lines.extend(preserved)
    else:
        for k in ("CPU_SOC", "DPDK_URL", "ACCEL_BASE", "DPDK_DIR", "DPDK_BUILD",
                  "POWER_SOURCE", "PROFILER"):
            lines.append(f"{k}={cfg[k]}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {out}")

    cfg = load_config()
    ok_all = True
    for role, host in (("DUT", dut), ("PKTGEN", pktgen)):
        if not host:
            continue
        ok, info = verify_ssh(cfg, host)
        log(f"{role} {'OK' if ok else 'SSH FAILED'}: {host} ({info})")
        ok_all = ok_all and ok
    return 0 if ok_all else 2


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
def cmd_install(cfg: dict, args: argparse.Namespace) -> int:
    dut = cfg.get("DUT_HOST")
    if not dut:
        log("Missing DUT_HOST - run: accel.py discover <DUT_IP>")
        return 1
    pw = ssh_pass(cfg, dut)
    user = cfg["SSH_USER"]
    base = expand_home(cfg["ACCEL_BASE"])
    url = cfg["DPDK_URL"]
    ver = cfg["DPDK_VERSION"]

    log("DUT: ensuring build + measurement tools")
    deps = ("meson ninja-build build-essential python3-pyelftools libnuma-dev "
            "libssl-dev pkg-config curl linux-tools-common linux-tools-generic "
            "ipmitool")
    run_remote(dut, user, pw,
               sudo(pw, "apt-get update -qq") + " && "
               + sudo(pw, f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {deps}")
               + " || true", 1200)

    install_sh = f"""#!/bin/bash
set -e
BASE={base}
URL={url}
VER={ver}
mkdir -p "$BASE" && cd "$BASE"
if [[ ! -f dpdk-$VER.tar.xz ]]; then curl -fL -o dpdk-$VER.tar.xz "$URL"; fi
if [[ ! -d dpdk-$VER ]]; then tar xf dpdk-$VER.tar.xz; fi
cd dpdk-$VER
if [[ ! -d build ]]; then meson setup build -Dexamples=dma; fi
ninja -C build
test -x build/app/dpdk-test-dma-perf && echo HAVE_DMA_PERF || echo NO_DMA_PERF
test -x build/app/dpdk-test-crypto-perf && echo HAVE_CRYPTO_PERF || echo NO_CRYPTO_PERF
echo DPDK_BUILD_DONE
"""
    log(f"DUT: building DPDK {ver} (several minutes)")
    code, out = run_remote_script(dut, user, pw, install_sh, 3600)
    print(out[-2000:])
    if "DPDK_BUILD_DONE" not in out:
        log(f"DUT: build did not complete (code={code})")
        return 1
    log("install complete")
    return 0


# --------------------------------------------------------------------------
# run / report / compare / email
# --------------------------------------------------------------------------
def cmd_run(cfg: dict, args: argparse.Namespace) -> int:
    cmd = [_py(), str(SCRIPTS / "run-accel-benchmark.py")]
    if args.accel:
        cmd += ["--accel", args.accel]
    if args.duration:
        cmd += ["--duration", str(args.duration)]
    if args.max_threads:
        cmd += ["--max-threads", str(args.max_threads)]
    if args.force_stubs:
        cmd += ["--force-stubs"]
    return subprocess.call(cmd)


def cmd_report(cfg: dict, args: argparse.Namespace) -> int:
    return subprocess.call([_py(), str(SCRIPTS / "analyze-accel-run.py"), "--latest", "--enrich"])


def cmd_compare(cfg: dict, args: argparse.Namespace) -> int:
    return subprocess.call([_py(), str(SCRIPTS / "compare-accel-runs.py"), *args.compare_args])


def cmd_email(cfg: dict, args: argparse.Namespace) -> int:
    rc = subprocess.call([_py(), str(SCRIPTS / "analyze-accel-run.py"), "--latest"])
    if rc not in (0, 2):
        log("analyze failed; not sending email")
        return rc
    return subprocess.call([_py(), str(SCRIPTS / "send-report-smtp.py")])


# --------------------------------------------------------------------------
# all
# --------------------------------------------------------------------------
def cmd_all(cfg: dict, args: argparse.Namespace) -> int:
    if not args.hosts:
        log("usage: accel.py all <DUT_IP> [PKTGEN_IP]")
        return 1
    rc = cmd_discover(cfg, argparse.Namespace(hosts=args.hosts))
    if rc != 0:
        return rc
    cfg = load_config()
    cmd_install(cfg, argparse.Namespace())
    cmd_run(cfg, argparse.Namespace(accel=None, duration=None, max_threads=None, force_stubs=False))
    cmd_report(cfg, argparse.Namespace())
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="accel.py", description="accelerator benchmark orchestrator")
    sub = ap.add_subparsers(dest="phase", required=True)

    p = sub.add_parser("discover", help="write config/lab.hosts and verify SSH")
    p.add_argument("hosts", nargs="*", help="<DUT_IP> [PKTGEN_IP]")

    sub.add_parser("install", help="build DPDK + ensure perf/turbostat/ipmitool")

    p = sub.add_parser("run", help="run benchmarks")
    p.add_argument("--accel", help="comma-separated accelerator names")
    p.add_argument("--duration", type=int)
    p.add_argument("--max-threads", type=int)
    p.add_argument("--force-stubs", action="store_true")

    sub.add_parser("report", help="analyze latest run -> executive summary")

    p = sub.add_parser("compare", help="cross-run comparison (passes args through)")
    p.add_argument("compare_args", nargs=argparse.REMAINDER)

    sub.add_parser("email", help="analyze + send summary via SMTP")

    p = sub.add_parser("all", help="discover -> install -> run -> report")
    p.add_argument("hosts", nargs="*", help="<DUT_IP> [PKTGEN_IP]")
    return ap


def main() -> int:
    # `compare` forwards all of its arguments to compare-accel-runs.py.
    # Handle it before argparse so options like --mode/--filter (which start
    # with '--') are passed through verbatim instead of being rejected here.
    argv = sys.argv[1:]
    if argv and argv[0] == "compare":
        return subprocess.call([_py(), str(SCRIPTS / "compare-accel-runs.py"), *argv[1:]])

    args = build_parser().parse_args()
    cfg = load_config()
    handlers = {
        "discover": cmd_discover,
        "install": cmd_install,
        "run": cmd_run,
        "report": cmd_report,
        "compare": cmd_compare,
        "email": cmd_email,
        "all": cmd_all,
    }
    return handlers[args.phase](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())
