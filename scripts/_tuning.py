#!/usr/bin/env python3
"""Shared AMD platform tuning probe + evaluation.

Probes a DUT over SSH for GRUB / BIOS-observable / power / PCIe / system
configuration, detects the EPYC SoC family, and compares each item to the
per-family profile in config/amd-tuning.json (see
https://doc.dpdk.org/guides/linux_gsg/amd_platform.html).

Used by both check-platform-tuning.py (the `tune` phase, with apply/reboot/
redfish modes and report rendering) and run-accel-benchmark.py (captures a
snapshot into every run record so the summary can show tuning vs the guide).
"""
from __future__ import annotations

import re

import _accel_common as store
from _lab_common import log, run_remote_script

# Severity -> status label used when an observed value does not match.
SEV_STATUS = {"fail": "FAIL", "warn": "WARN", "info": "INFO"}


# --------------------------------------------------------------------------
# family detection
# --------------------------------------------------------------------------
def detect_family(model: str, tuning: dict, cfg_soc: str) -> str | None:
    num = ""
    m = re.search(r"EPYC\s+(\w?\d{3,4})", model)
    if m:
        num = m.group(1)
    for entry in tuning.get("cpu_model_map", []):
        if num and re.search(entry["match"], num):
            return entry["family"]
    soc = (cfg_soc or "").strip().lower()
    families = tuning.get("families", {})
    if soc in families:
        return soc
    # Allow CPU_SOC like "turin-9745" to match the "turin" family.
    for fam in families:
        if fam in soc:
            return fam
    return None


# --------------------------------------------------------------------------
# remote probe
# --------------------------------------------------------------------------
def probe_script(bdfs: list[str]) -> str:
    pcie = "echo NONE"
    if bdfs:
        pcie = "; ".join(
            f"printf '%s ' '{b}'; lspci -s '{b}' -vv 2>/dev/null "
            f"| grep -m1 'LnkSta:' || echo 'LnkSta: n/a'"
            for b in bdfs
        )
    return f"""#!/bin/bash
echo '===CMDLINE==='; cat /proc/cmdline
echo '===MODEL==='; lscpu | grep -E 'Model name|^Socket\\(s\\)|Thread\\(s\\) per core|^NUMA node\\(s\\)'
echo '===KERNEL==='; uname -r
echo '===PSTATE==='; cat /sys/devices/system/cpu/amd_pstate/status 2>/dev/null || echo n/a
echo '===DRIVER==='; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null || echo n/a
echo '===GOV==='; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a
echo '===BOOST==='; cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null || echo n/a
echo '===IOMMU==='; ls /sys/class/iommu 2>/dev/null | wc -l
echo '===X2APIC==='; grep -m1 -o x2apic /proc/cpuinfo 2>/dev/null || echo no
echo '===HSMP==='; (lsmod | grep -q amd_hsmp && echo loaded) || (test -e /dev/hsmp && echo loaded) || echo absent
echo '===THP==='; cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo n/a
echo '===HP1G==='; cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null || echo n/a
echo '===TUNED==='; tuned-adm active 2>/dev/null || echo n/a
echo '===PCIE==='; {pcie}
echo
echo '===END==='
"""


def _section(raw: str, name: str) -> str:
    m = re.search(rf"==={name}===\n(.*?)(?=\n===|\Z)", raw, re.S)
    return m.group(1).strip() if m else ""


def parse_probe(raw: str) -> dict:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")  # PTY adds CRLF
    cmdline = _section(raw, "CMDLINE")
    model_blk = _section(raw, "MODEL")

    def grab(label: str) -> str:
        m = re.search(rf"{label}\s*:\s*(.+)", model_blk)
        return m.group(1).strip() if m else ""

    model = grab("Model name")
    sockets = int(re.sub(r"\D", "", grab(r"Socket\(s\)")) or "1")
    tpc = int(re.sub(r"\D", "", grab(r"Thread\(s\) per core")) or "1")
    numa = int(re.sub(r"\D", "", grab(r"NUMA node\(s\)")) or "1")

    cs = re.search(r"processor\.max_cstate=(\d+)", cmdline)
    iommu_cnt = int((_section(raw, "IOMMU") or "0").split()[0] or "0")

    # transparent_hugepage line looks like "always madvise [never]"
    thp_blk = _section(raw, "THP")
    thp = "n/a"
    mt = re.search(r"\[(\w+)\]", thp_blk)
    if mt:
        thp = mt.group(1)
    elif thp_blk:
        thp = thp_blk.split()[0]

    hp1g_blk = (_section(raw, "HP1G") or "n/a").strip()
    hp1g = hp1g_blk.split()[0] if hp1g_blk else "n/a"

    tuned_blk = _section(raw, "TUNED")
    tm = re.search(r"Current active profile:\s*(.+)", tuned_blk)
    tuned = tm.group(1).strip() if tm else (tuned_blk.strip() or "n/a")

    pcie: list[dict] = []
    pcie_blk = _section(raw, "PCIE")
    if pcie_blk and "NONE" not in pcie_blk:
        for line in pcie_blk.splitlines():
            mb = re.match(r"(\S+)\s+LnkSta:\s*(.*)", line.strip())
            if not mb:
                continue
            bdf, sta = mb.group(1), mb.group(2)
            sp = re.search(r"Speed\s+([\d.]+)GT/s", sta)
            wd = re.search(r"Width\s+x(\d+)", sta)
            pcie.append({
                "bdf": bdf,
                "speed_gts": float(sp.group(1)) if sp else 0.0,
                "width": int(wd.group(1)) if wd else 0,
                "raw": sta,
            })

    return {
        "cmdline": cmdline,
        "model": model,
        "kernel": (_section(raw, "KERNEL") or "n/a").strip(),
        "sockets": sockets,
        "threads_per_core": tpc,
        "numa_nodes": numa,
        "smt": "on" if tpc >= 2 else "off",
        "nps": str(numa // sockets if sockets else numa),
        "max_cstate": cs.group(1) if cs else "n/a",
        "boost": (_section(raw, "BOOST") or "n/a").strip(),
        "iommu": "on" if iommu_cnt > 0 else "off",
        "x2apic": "on" if "x2apic" in _section(raw, "X2APIC") else "off",
        "amd_pstate": (_section(raw, "PSTATE") or "n/a").strip(),
        "scaling_driver": (_section(raw, "DRIVER") or "n/a").strip(),
        "governor": (_section(raw, "GOV") or "n/a").strip(),
        "hsmp": (_section(raw, "HSMP") or "absent").strip(),
        "thp": thp,
        "hugepages_1g": hp1g,
        "tuned_profile": tuned,
        "pcie": pcie,
    }


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------
def _status(observed: str, expected: str, severity: str) -> str:
    o = (observed or "").strip().lower()
    e = (expected or "").strip().lower()
    if o in ("", "n/a"):
        return "WARN"
    if o == e or e in o:
        return "PASS"
    return SEV_STATUS.get(severity, "WARN")


def evaluate(profile: dict, common: dict, observed: dict, cfg: dict) -> list[dict]:
    checks: list[dict] = []

    # GRUB required kernel params (substring match against /proc/cmdline)
    cmdline = observed["cmdline"]
    for param in profile.get("grub", {}).get("required", []):
        present = param in cmdline
        checks.append({
            "category": "GRUB",
            "item": param,
            "expected": "present",
            "observed": "present" if present else "missing",
            "status": "PASS" if present else "FAIL",
            "remediation": "" if present
            else f"add '{param}' to GRUB_CMDLINE_LINUX (run --apply-grub)",
        })

    # OS-observable BIOS indicators
    for key, spec in profile.get("bios", {}).get("observable", {}).items():
        exp = str(spec.get("expect", ""))
        obs = str(observed.get(key, "n/a"))
        st = _status(obs, exp, spec.get("severity", "warn"))
        checks.append({
            "category": "BIOS",
            "item": key,
            "expected": exp,
            "observed": obs,
            "status": st,
            "remediation": "" if st == "PASS"
            else f"BIOS: {spec.get('note', 'set ' + key + '=' + exp)}",
        })

    # Power: amd_pstate mode + HSMP module
    power = profile.get("power", {})
    for key in ("amd_pstate", "hsmp"):
        spec = power.get(key)
        if not spec:
            continue
        exp = str(spec.get("expect", ""))
        obs = str(observed.get(key, "n/a"))
        st = _status(obs, exp, spec.get("severity", "info"))
        checks.append({
            "category": "POWER",
            "item": key,
            "expected": exp,
            "observed": obs,
            "status": st,
            "remediation": "" if st == "PASS"
            else f"POWER: {spec.get('note', 'set ' + key + '=' + exp)}",
        })

    # System config (kernel, THP, 1G hugepages, tuned profile)
    sysspec = common.get("system", {})
    checks.append({
        "category": "SYSTEM", "item": "kernel", "expected": "-",
        "observed": str(observed.get("kernel", "n/a")), "status": "INFO",
        "remediation": "",
    })
    if "thp" in sysspec:
        spec = sysspec["thp"]
        exp = str(spec.get("expect", "never"))
        obs = str(observed.get("thp", "n/a"))
        st = _status(obs, exp, spec.get("severity", "warn"))
        checks.append({
            "category": "SYSTEM", "item": "transparent_hugepage",
            "expected": exp, "observed": obs, "status": st,
            "remediation": "" if st == "PASS"
            else f"SYSTEM: {spec.get('note', 'set transparent_hugepage=' + exp)}",
        })
    if "hugepages_1g" in sysspec:
        spec = sysspec["hugepages_1g"]
        minhp = int(spec.get("min", 1))
        raw = str(observed.get("hugepages_1g", "n/a"))
        try:
            ok = int(raw) >= minhp
            st = "PASS" if ok else SEV_STATUS.get(spec.get("severity", "warn"), "WARN")
        except ValueError:
            st = "WARN"
        checks.append({
            "category": "SYSTEM", "item": "1G hugepages reserved",
            "expected": f">={minhp}", "observed": raw, "status": st,
            "remediation": "" if st == "PASS"
            else f"SYSTEM: {spec.get('note', 'reserve 1G hugepages')}",
        })
    if "tuned_profile" in sysspec:
        spec = sysspec["tuned_profile"]
        rec = [str(x).lower() for x in spec.get("recommended", [])]
        obs = str(observed.get("tuned_profile", "n/a"))
        st = "PASS" if obs.lower() in rec else SEV_STATUS.get(
            spec.get("severity", "info"), "INFO")
        checks.append({
            "category": "SYSTEM", "item": "tuned profile",
            "expected": "/".join(spec.get("recommended", [])) or "-",
            "observed": obs, "status": st,
            "remediation": "" if st == "PASS"
            else f"SYSTEM: {spec.get('note', 'apply a performance tuned profile')}",
        })

    # PCIe link speed/width for probed BDFs
    pcie_req = profile.get("pcie", {})
    min_sp = float(pcie_req.get("min_speed_gts", 0))
    min_wd = int(pcie_req.get("min_width", 0))
    for dev in observed.get("pcie", []):
        ok = dev["speed_gts"] >= min_sp and dev["width"] >= min_wd
        checks.append({
            "category": "PCIE",
            "item": dev["bdf"],
            "expected": f">={min_sp}GT/s x{min_wd}",
            "observed": f"{dev['speed_gts']}GT/s x{dev['width']}",
            "status": "PASS" if ok else "WARN",
            "remediation": "" if ok
            else f"PCIE: {pcie_req.get('note', 'use a faster slot')}",
        })
    return checks


def overall_verdict(checks: list[dict]) -> str:
    statuses = {c["status"] for c in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def diff_count(checks: list[dict]) -> int:
    """Number of checks that differ from the guide (WARN or FAIL)."""
    return sum(1 for c in checks if c["status"] in ("WARN", "FAIL"))


# --------------------------------------------------------------------------
# GRUB configurator (shared by the tune checker and the sanity remediation)
# --------------------------------------------------------------------------
def recommended_grub_line(profile: dict, cfg: dict) -> str:
    grub = profile.get("grub", {})
    line = grub.get("apply_base", "").format(
        hugepages=cfg.get("TUNE_HUGEPAGES", "64"))
    isol = cfg.get("TUNE_ISOLCPUS", "").strip()
    if isol and grub.get("isolation_template"):
        line += " " + grub["isolation_template"].format(isolcpus=isol)
    return line


def apply_grub(host: str, user: str, pw: str, line: str) -> bool:
    """Rewrite GRUB_CMDLINE_LINUX and regenerate GRUB. Returns reboot-needed.

    Backs up /etc/default/grub first (matching brcm-ptp apply-kernel-tuning.py).
    """
    log(f"  applying GRUB_CMDLINE_LINUX on {host}")
    grubfix = (
        "import re\n"
        "p='/etc/default/grub'\n"
        "s=open(p).read()\n"
        f"new='GRUB_CMDLINE_LINUX=\"{line}\"'\n"
        "if re.search(r'^GRUB_CMDLINE_LINUX=.*$', s, flags=re.M):\n"
        "    s=re.sub(r'^GRUB_CMDLINE_LINUX=.*$', new, s, flags=re.M)\n"
        "else:\n"
        "    s=s.rstrip()+'\\n'+new+'\\n'\n"
        "open(p,'w').write(s)\n"
        "print('grub-updated')\n"
    )
    script = f"""#!/bin/bash
PW='{pw}'
ts=$(date +%s)
echo "$PW" | sudo -S cp /etc/default/grub /etc/default/grub.bak.$ts
cat > /tmp/_grubfix.py <<'PYEOF'
{grubfix}
PYEOF
echo "$PW" | sudo -S python3 /tmp/_grubfix.py
echo "$PW" | sudo -S update-grub 2>&1 | tail -3 || echo "$PW" | sudo -S grub-mkconfig -o /boot/grub/grub.cfg 2>&1 | tail -3
echo '-- configured --'
grep '^GRUB_CMDLINE_LINUX=' /etc/default/grub
echo '-- running --'
cat /proc/cmdline
"""
    _, out = run_remote_script(host, user, pw, script, 180)
    print(out)
    running = out.split("-- running --")[-1]
    # Reboot needed if a key param is configured but not yet running.
    return "pcie_aspm=off" not in running


# --------------------------------------------------------------------------
# one-shot snapshot
# --------------------------------------------------------------------------
def snapshot(cfg: dict, tuning: dict, host: str, user: str, pw: str,
             bdfs: list[str] | None = None) -> dict:
    """Probe the host and return a tuning record (no report rendering)."""
    bdfs = bdfs or []
    _, raw = run_remote_script(host, user, pw, probe_script(bdfs), 90)
    observed = parse_probe(raw)
    family = detect_family(observed["model"], tuning, cfg.get("CPU_SOC", ""))
    if not family:
        family = "turin"
    profile = tuning.get("families", {}).get(family, {})
    checks = evaluate(profile, tuning.get("common", {}), observed, cfg)
    return {
        "host": host,
        "model": observed["model"],
        "family": family,
        "generated": store.now_iso(),
        "git_commit": store.git_commit(),
        "observed": observed,
        "checks": checks,
        "verdict": overall_verdict(checks),
        "redfish": [],
    }
