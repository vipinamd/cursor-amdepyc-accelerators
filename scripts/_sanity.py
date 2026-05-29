#!/usr/bin/env python3
"""Setup sanity preflight: compiler / DPDK build / hugepages / device / config.

Probes a DUT for the prerequisites a benchmark needs, rolls up the BIOS/GRUB
state from the platform tuning snapshot (see _tuning.py), and classifies each
item PASS/WARN/FAIL with a `blocker` flag (a blocker makes the test impossible).

With remediation enabled (the runner's --fix), it self-heals by reusing the
brcm-ptp configurator pattern: install the toolchain, build DPDK, apply the
recommended GRUB line (reboot), reserve hugepages, and bind the device to
vfio-pci. BIOS settings are never changed (report-only).

Shared by check-setup-sanity.py (the `preflight` phase) and
run-accel-benchmark.py (captures a snapshot into every run record).
"""
from __future__ import annotations

import re

import _accel_common as store
import _tuning as tuning_lib
from _lab_common import expand_home, log, run_remote, run_remote_script, sudo
from plugins import get_plugin

# DPDK test app binary required per tool (None = in-process, no DPDK build).
TOOL_APP = {
    "dma_perf": "dpdk-test-dma-perf",
    "crypto_perf": "dpdk-test-crypto-perf",
    "eventdev": "dpdk-test-eventdev",
    "memcpy_ref": None,
}

# apt packages that provide the toolchain + measurement tools (matches
# accel.py cmd_install so --fix and `install` stay consistent).
TOOLCHAIN_DEPS = (
    "meson ninja-build build-essential python3-pyelftools libnuma-dev "
    "libssl-dev pkg-config curl linux-tools-common linux-tools-generic ipmitool"
)


# --------------------------------------------------------------------------
# remote probe
# --------------------------------------------------------------------------
def _probe_script(app_path: str | None, bdf: str) -> str:
    app = (f'test -x "{app_path}" && echo present || echo missing'
           if app_path else "echo n/a")
    drv = (f"(test -e /sys/bus/pci/devices/{bdf}/driver && "
           f"basename $(readlink /sys/bus/pci/devices/{bdf}/driver)) "
           f"2>/dev/null || echo none") if bdf else "echo none"
    return f"""#!/bin/bash
echo '===GCC==='; gcc -dumpversion 2>/dev/null || echo missing
echo '===MESON==='; meson --version 2>/dev/null || echo missing
echo '===NINJA==='; ninja --version 2>/dev/null || echo missing
echo '===PKGCONFIG==='; pkg-config --version 2>/dev/null || echo missing
echo '===DPDKAPP==='; {app}
echo '===HP1GFREE==='; cat /sys/kernel/mm/hugepages/hugepages-1048576kB/free_hugepages 2>/dev/null || echo n/a
echo '===HP2MFREE==='; cat /sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages 2>/dev/null || echo n/a
echo '===DRIVER==='; {drv}
echo '===TURBOSTAT==='; command -v turbostat >/dev/null && echo yes || echo no
echo '===IPMITOOL==='; command -v ipmitool >/dev/null && echo yes || echo no
echo '===PERF==='; command -v perf >/dev/null && echo yes || echo no
echo '===END==='
"""


def _section(raw: str, name: str) -> str:
    m = re.search(rf"==={name}===\n(.*?)(?=\n===|\Z)", raw, re.S)
    return m.group(1).strip() if m else ""


def _app_path(cfg: dict, tool: str) -> str | None:
    app = TOOL_APP.get(tool)
    if not app:
        return None
    dpdk = expand_home(cfg["DPDK_DIR"])
    build = cfg.get("DPDK_BUILD", "build")
    return f"{dpdk}/{build}/app/{app}"


def probe(cfg: dict, accel_cfg: dict, tool: str, host: str, user: str,
          pw: str) -> dict:
    bdf = accel_cfg.get("bdf", "") if accel_cfg.get("mode", "dma") != "cpu" else ""
    _, raw = run_remote_script(
        host, user, pw, _probe_script(_app_path(cfg, tool), bdf), 60)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return {
        "gcc": (_section(raw, "GCC") or "missing").splitlines()[0],
        "meson": (_section(raw, "MESON") or "missing").splitlines()[0],
        "ninja": (_section(raw, "NINJA") or "missing").splitlines()[0],
        "pkg_config": (_section(raw, "PKGCONFIG") or "missing").splitlines()[0],
        "dpdk_app": (_section(raw, "DPDKAPP") or "missing").strip(),
        "hp_1g_free": (_section(raw, "HP1GFREE") or "n/a").strip(),
        "hp_2m_free": (_section(raw, "HP2MFREE") or "n/a").strip(),
        "driver": (_section(raw, "DRIVER") or "none").strip(),
        "turbostat": (_section(raw, "TURBOSTAT") or "no").strip(),
        "ipmitool": (_section(raw, "IPMITOOL") or "no").strip(),
        "perf": (_section(raw, "PERF") or "no").strip(),
    }


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------
def _row(category, item, expected, observed, status, blocker=False,
         remediation="", fix="") -> dict:
    return {"category": category, "item": item, "expected": expected,
            "observed": observed, "status": status, "blocker": blocker,
            "remediation": remediation, "fix": fix}


def evaluate(cfg: dict, accel_cfg: dict, tool: str, pr: dict,
             tuning_snap: dict, sanity_cfg: dict) -> list[dict]:
    rows: list[dict] = []
    app_needed = TOOL_APP.get(tool) is not None
    dma_mode = accel_cfg.get("mode", "dma") != "cpu" and bool(accel_cfg.get("bdf"))
    vfio = sanity_cfg.get("vfio_driver", "vfio-pci")

    # Compiler / toolchain (needed only to build; WARN, fixable via install)
    for item, key in (("gcc", "gcc"), ("meson", "meson"),
                      ("ninja", "ninja"), ("pkg-config", "pkg_config")):
        obs = pr.get(key, "missing")
        ok = obs not in ("", "missing")
        rows.append(_row("COMPILER", item, "present", obs,
                         "PASS" if ok else "WARN",
                         remediation="" if ok else f"install {item}",
                         fix="" if ok else "install_toolchain"))

    # DPDK build / app binary
    if app_needed:
        present = pr.get("dpdk_app") == "present"
        rows.append(_row("BUILD", TOOL_APP[tool], "present",
                         pr.get("dpdk_app", "missing"),
                         "PASS" if present else "FAIL", blocker=not present,
                         remediation="" if present
                         else "build DPDK (accel.py install or --fix)",
                         fix="" if present else "build_dpdk"))
    else:
        rows.append(_row("BUILD", "dpdk app", "n/a (in-process)", "n/a", "INFO"))

    # Hugepages (DPDK EAL needs free hugepages; in-process memcpy does not)
    if app_needed:
        mode = cfg.get("HUGEPAGE_MODE", "2m").lower()
        free_raw = pr.get("hp_1g_free") if mode == "1g" else pr.get("hp_2m_free")
        minhp = int(sanity_cfg.get("min_free_hugepages", 1))
        try:
            ok = int(free_raw) >= minhp
        except (TypeError, ValueError):
            ok = False
        rows.append(_row("HUGEPAGES", f"free {mode} pages", f">={minhp}",
                         str(free_raw), "PASS" if ok else "FAIL",
                         blocker=not ok,
                         remediation="" if ok else f"reserve {mode} hugepages",
                         fix="" if ok else "reserve_hugepages"))

    # Device binding (only for hardware DMA mode with a bdf)
    if dma_mode:
        drv = pr.get("driver", "none")
        ok = drv == vfio
        rows.append(_row("DEVICE", accel_cfg["bdf"], vfio, drv,
                         "PASS" if ok else "FAIL", blocker=not ok,
                         remediation="" if ok else f"bind {accel_cfg['bdf']} to {vfio}",
                         fix="" if ok else "bind_vfio"))

    # Configuration validity
    try:
        implemented = get_plugin(tool).implemented
    except KeyError:
        implemented = False
    rows.append(_row("CONFIG", "tool", "implemented",
                     "yes" if implemented else "no",
                     "PASS" if implemented else "FAIL", blocker=not implemented,
                     remediation="" if implemented else f"no plugin for tool '{tool}'"))
    if dma_mode and not accel_cfg.get("bdf"):
        rows.append(_row("CONFIG", "device bdf", "set", "missing", "FAIL",
                         blocker=True, remediation="set bdf in accelerators.json"))

    # BIOS / GRUB rolled up from the tuning snapshot
    checks = tuning_snap.get("checks", []) if tuning_snap else []
    grub = [c for c in checks if c["category"] == "GRUB"]
    grub_missing = [c for c in grub if c["status"] != "PASS"]
    if grub:
        # IOMMU passthrough matters for vfio binding -> blocker in DMA mode.
        st = "PASS" if not grub_missing else ("FAIL" if dma_mode else "WARN")
        rows.append(_row("GRUB", "kernel params", "all present",
                         f"{len(grub_missing)} missing" if grub_missing else "all present",
                         st, blocker=bool(grub_missing) and dma_mode,
                         remediation="" if not grub_missing
                         else "apply recommended GRUB line (accel.py tune --apply-grub)",
                         fix="" if not grub_missing else "apply_grub"))
    bios = [c for c in checks if c["category"] == "BIOS"]
    bios_diff = [c for c in bios if c["status"] != "PASS"]
    if bios:
        rows.append(_row("BIOS", "settings vs guide",
                         "match", f"{len(bios_diff)} differ" if bios_diff else "match",
                         "PASS" if not bios_diff else "WARN",
                         remediation="" if not bios_diff
                         else "set in firmware/BMC (see accel.py tune)"))

    # Measurement tools (non-blocking; affect power/profiler only)
    for item, key in (("turbostat", "turbostat"), ("ipmitool", "ipmitool"),
                      ("perf", "perf")):
        ok = pr.get(key) == "yes"
        rows.append(_row("TOOLS", item, "present", pr.get(key, "no"),
                         "PASS" if ok else "WARN",
                         remediation="" if ok else f"install {item} (accel.py install)",
                         fix="" if ok else "install_toolchain"))
    return rows


def overall(rows: list[dict]) -> tuple[str, bool]:
    statuses = {r["status"] for r in rows}
    has_blocker = any(r["blocker"] and r["status"] == "FAIL" for r in rows)
    if "FAIL" in statuses:
        return "FAIL", has_blocker
    if "WARN" in statuses:
        return "WARN", has_blocker
    return "PASS", has_blocker


def blocker_count(rows: list[dict]) -> int:
    return sum(1 for r in rows if r["blocker"] and r["status"] == "FAIL")


def diff_count(rows: list[dict]) -> int:
    return sum(1 for r in rows if r["status"] in ("WARN", "FAIL"))


# --------------------------------------------------------------------------
# remediation (brcm-ptp configurator pattern) - only under --fix
# --------------------------------------------------------------------------
def install_toolchain(host: str, user: str, pw: str) -> bool:
    log(f"  [fix] installing toolchain/tools on {host}")
    code, _ = run_remote(
        host, user, pw,
        sudo(pw, "apt-get update -qq") + " && "
        + sudo(pw, f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {TOOLCHAIN_DEPS}")
        + " || true", 1200)
    return code == 0


def build_dpdk(cfg: dict, host: str, user: str, pw: str) -> bool:
    log(f"  [fix] building DPDK on {host} (several minutes)")
    base = expand_home(cfg["ACCEL_BASE"])
    url, ver = cfg["DPDK_URL"], cfg["DPDK_VERSION"]
    build = cfg.get("DPDK_BUILD", "build")
    script = f"""#!/bin/bash
set -e
BASE={base}; URL={url}; VER={ver}
mkdir -p "$BASE" && cd "$BASE"
if [[ ! -f dpdk-$VER.tar.xz ]]; then curl -fL -o dpdk-$VER.tar.xz "$URL"; fi
if [[ ! -d dpdk-$VER ]]; then tar xf dpdk-$VER.tar.xz; fi
cd dpdk-$VER
if [[ ! -d {build} ]]; then meson setup {build} -Dexamples=dma; fi
ninja -C {build}
echo DPDK_BUILD_DONE
"""
    _, out = run_remote_script(host, user, pw, script, 3600)
    return "DPDK_BUILD_DONE" in out


def reserve_hugepages(cfg: dict, host: str, user: str, pw: str,
                      count: int = 0) -> bool:
    mode = cfg.get("HUGEPAGE_MODE", "2m").lower()
    sub = "hugepages-1048576kB" if mode == "1g" else "hugepages-2048kB"
    n = count or int(cfg.get("TUNE_HUGEPAGES", "64"))
    log(f"  [fix] reserving {n} {mode} hugepages on {host}")
    path = f"/sys/kernel/mm/hugepages/{sub}/nr_hugepages"
    code, _ = run_remote(host, user, pw,
                         sudo(pw, f"bash -c 'echo {n} > {path}'") + " || true", 60)
    return code == 0


def bind_vfio(cfg: dict, host: str, user: str, pw: str, bdf: str,
              driver: str = "vfio-pci") -> bool:
    log(f"  [fix] binding {bdf} to {driver} on {host}")
    dpdk = expand_home(cfg["DPDK_DIR"])
    script = f"""#!/bin/bash
PW='{pw}'
echo "$PW" | sudo -S modprobe {driver} 2>&1 || echo "$PW" | sudo -S modprobe vfio enable_unsafe_noiommu_mode=1 2>&1 || true
DEVBIND="{dpdk}/usertools/dpdk-devbind.py"
if [[ -x "$DEVBIND" || -f "$DEVBIND" ]]; then
  echo "$PW" | sudo -S python3 "$DEVBIND" --bind={driver} {bdf} 2>&1
else
  echo "$PW" | sudo -S dpdk-devbind.py --bind={driver} {bdf} 2>&1 || echo NO_DEVBIND
fi
basename $(readlink /sys/bus/pci/devices/{bdf}/driver) 2>/dev/null || echo none
"""
    _, out = run_remote_script(host, user, pw, script, 120)
    print(out)
    return driver in out.splitlines()[-1] if out.strip() else False


def remediate(rows: list[dict], cfg: dict, accel_cfg: dict, tuning_snap: dict,
              tuning_cfg: dict, host: str, user: str, pw: str) -> tuple[list[str], bool]:
    """Apply fixes for non-PASS rows. Returns (applied_labels, reboot_needed)."""
    needed = {r["fix"] for r in rows if r["status"] != "PASS" and r.get("fix")}
    applied: list[str] = []
    reboot_needed = False

    if "install_toolchain" in needed:
        if install_toolchain(host, user, pw):
            applied.append("install toolchain/tools")
    if "build_dpdk" in needed:
        if build_dpdk(cfg, host, user, pw):
            applied.append("build DPDK")
    if "reserve_hugepages" in needed:
        if reserve_hugepages(cfg, host, user, pw):
            applied.append("reserve hugepages")
    if "apply_grub" in needed:
        family = (tuning_snap or {}).get("family", "")
        profile = tuning_cfg.get("families", {}).get(family, {})
        line = tuning_lib.recommended_grub_line(profile, cfg)
        if line and tuning_lib.apply_grub(host, user, pw, line):
            applied.append("apply GRUB line")
            reboot_needed = True
    if "bind_vfio" in needed and accel_cfg.get("bdf"):
        drv = (tuning_cfg.get("common", {}) and "vfio-pci") or "vfio-pci"
        if bind_vfio(cfg, host, user, pw, accel_cfg["bdf"], drv):
            applied.append(f"bind {accel_cfg['bdf']} to vfio-pci")
    return applied, reboot_needed


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------
def snapshot(cfg: dict, sanity_cfg: dict, accel: str, accel_cfg: dict,
             tool: str, host: str, user: str, pw: str,
             tuning_snap: dict) -> dict:
    pr = probe(cfg, accel_cfg, tool, host, user, pw)
    rows = evaluate(cfg, accel_cfg, tool, pr, tuning_snap, sanity_cfg)
    verdict, has_blocker = overall(rows)
    return {
        "host": host,
        "accelerator": accel,
        "tool": tool,
        "generated": store.now_iso(),
        "git_commit": store.git_commit(),
        "probe": pr,
        "rows": rows,
        "verdict": verdict,
        "blocker": has_blocker,
        "remediated": [],
        "rebooted": False,
    }
