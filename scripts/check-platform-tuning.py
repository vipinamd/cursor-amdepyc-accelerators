#!/usr/bin/env python3
"""Check an AMD EPYC DUT against the DPDK AMD platform tuning guide.

Verifies GRUB kernel parameters, OS-observable BIOS indicators (SMT, NPS,
C-states, boost, IOMMU, x2APIC, governor), power setup (amd_pstate, HSMP),
and PCIe link speed against the per-family profile in config/amd-tuning.json
(see https://doc.dpdk.org/guides/linux_gsg/amd_platform.html). Produces a
PASS/WARN/FAIL tuning report (JSON + MD/TXT/HTML).

Read-only by default. Optional, explicit-flag actions:
  --apply-grub     rewrite GRUB_CMDLINE_LINUX with the recommended line
  --reboot         reboot the host and wait for it to return, then re-check
  --bios-redfish   query the BMC Redfish BIOS attributes (needs DUT_BMC_IP)

Usage:
  python check-platform-tuning.py                 # check the DUT
  python check-platform-tuning.py --bios-redfish
  python check-platform-tuning.py --apply-grub --reboot
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _accel_common as store
from _lab_common import (
    REPORTS, RESULTS, load_config, log, reboot_host, run_remote,
    run_remote_script, ssh_pass, sudo, wait_for_ssh,
)
from _render import ascii_table, html_table, md_table

TUNING_DIR = RESULTS / "tuning"

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
def _probe_script(bdfs: list[str]) -> str:
    pcie = "echo NONE"
    if bdfs:
        loops = "; ".join(
            f"printf '%s ' '{b}'; lspci -s '{b}' -vv 2>/dev/null "
            f"| grep -m1 'LnkSta:' || echo 'LnkSta: n/a'"
            for b in bdfs
        )
        pcie = loops
    return f"""#!/bin/bash
echo '===CMDLINE==='; cat /proc/cmdline
echo '===MODEL==='; lscpu | grep -E 'Model name|^Socket\\(s\\)|Thread\\(s\\) per core|^NUMA node\\(s\\)'
echo '===PSTATE==='; cat /sys/devices/system/cpu/amd_pstate/status 2>/dev/null || echo n/a
echo '===DRIVER==='; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null || echo n/a
echo '===GOV==='; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a
echo '===BOOST==='; cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null || echo n/a
echo '===IOMMU==='; ls /sys/class/iommu 2>/dev/null | wc -l
echo '===X2APIC==='; grep -m1 -o x2apic /proc/cpuinfo 2>/dev/null || echo no
echo '===HSMP==='; (lsmod | grep -q amd_hsmp && echo loaded) || (test -e /dev/hsmp && echo loaded) || echo absent
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


def evaluate(profile: dict, observed: dict, cfg: dict) -> list[dict]:
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
    for key, obskey in (("amd_pstate", "amd_pstate"), ("hsmp", "hsmp")):
        spec = power.get(key)
        if not spec:
            continue
        exp = str(spec.get("expect", ""))
        obs = str(observed.get(obskey, "n/a"))
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


# --------------------------------------------------------------------------
# optional: apply GRUB / reboot / Redfish BIOS
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
    """Rewrite GRUB_CMDLINE_LINUX and regenerate GRUB. Returns reboot-needed."""
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


def redfish_bios(cfg: dict, profile: dict, tuning: dict) -> list[dict]:
    """Query BMC Redfish BIOS attributes from the DUT and compare to profile."""
    bmc = cfg.get("DUT_BMC_IP", "").strip()
    if not bmc:
        log("  --bios-redfish: DUT_BMC_IP not set; skipping Redfish check")
        return []
    host = cfg["DUT_HOST"]
    user = cfg["SSH_USER"]
    pw = ssh_pass(cfg, host)
    bu, bp = cfg.get("BMC_USER", ""), cfg.get("BMC_PASS", "")
    base = f"https://{bmc}/redfish/v1"
    # Discover the first system, then read its Bios Attributes (run curl on DUT).
    script = f"""#!/bin/bash
C="curl -sk --max-time 15 -u '{bu}:{bp}'"
sys=$(eval $C {base}/Systems 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["Members"][0]["@odata.id"])' 2>/dev/null)
[ -z "$sys" ] && {{ echo NO_SYSTEM; exit 0; }}
eval $C "https://{bmc}$sys/Bios" 2>/dev/null
"""
    _, out = run_remote_script(host, user, pw, script, 60)
    rows: list[dict] = []
    if "NO_SYSTEM" in out or not out.strip():
        log("  --bios-redfish: no Redfish system/BIOS found on BMC")
        return rows
    try:
        start = out.index("{")
        attrs = json.loads(out[start:]).get("Attributes", {})
    except (ValueError, json.JSONDecodeError):
        log("  --bios-redfish: could not parse Redfish BIOS response")
        return rows
    expected = tuning.get("common", {}).get("bios_redfish", {})
    lower = {k.lower(): (k, v) for k, v in attrs.items()}
    for want_name, want_val in expected.items():
        match = next((kv for lk, kv in lower.items()
                      if want_name.lower() in lk), None)
        if not match:
            rows.append({"attribute": want_name, "expected": want_val,
                         "observed": "not-present", "status": "INFO"})
            continue
        actual = str(match[1])
        ok = str(want_val).lower() in actual.lower()
        rows.append({"attribute": match[0], "expected": want_val,
                     "observed": actual, "status": "PASS" if ok else "WARN"})
    return rows


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def render_report(rec: dict, tuning: dict) -> tuple[str, str, str]:
    ov = [
        ["Host", rec["host"]],
        ["CPU model", rec["model"]],
        ["SoC family", rec["family"]],
        ["Verdict", rec["verdict"]],
        ["Git commit", rec["git_commit"]],
        ["Generated (UTC)", rec["generated"]],
    ]
    chk_hdr = ["Category", "Item", "Expected", "Observed", "Status"]
    chk_rows = [[c["category"], c["item"], c["expected"], c["observed"], c["status"]]
                for c in rec["checks"]]
    remed = [c["remediation"] for c in rec["checks"]
             if c["status"] != "PASS" and c["remediation"]]
    checklist = tuning.get("common", {}).get("bios_manual_checklist", [])
    cl_rows = [[c["setting"], c["value"]] for c in checklist]

    md = ["# AMD platform tuning report", "",
          f"- **Verdict:** {rec['verdict']}",
          f"- **Host / SoC:** {rec['host']} / {rec['family']}",
          f"- **CPU:** {rec['model']}",
          f"- **Generated (UTC):** {rec['generated']}", "",
          "## Checks", "", md_table(chk_hdr, chk_rows), ""]
    if rec.get("redfish"):
        rf_rows = [[r["attribute"], r["expected"], r["observed"], r["status"]]
                   for r in rec["redfish"]]
        md += ["## BIOS via Redfish", "",
               md_table(["Attribute", "Expected", "Observed", "Status"], rf_rows), ""]
    if remed:
        md += ["## Remediations", ""] + [f"- {r}" for r in remed] + [""]
    md += ["## BIOS manual checklist (set in firmware / BMC)", "",
           md_table(["Setting", "Recommended"], cl_rows), ""]
    md_text = "\n".join(md) + "\n"

    txt = ["AMD platform tuning report", "=" * 42, "",
           f"Verdict: {rec['verdict']}   Host: {rec['host']} ({rec['family']})",
           f"CPU: {rec['model']}", "",
           "CHECKS", ascii_table(chk_hdr, chk_rows), ""]
    if rec.get("redfish"):
        rf_rows = [[r["attribute"], r["expected"], r["observed"], r["status"]]
                   for r in rec["redfish"]]
        txt += ["BIOS via Redfish",
                ascii_table(["Attribute", "Expected", "Observed", "Status"], rf_rows), ""]
    if remed:
        txt += ["REMEDIATIONS"] + [f"  - {r}" for r in remed] + [""]
    txt += ["BIOS MANUAL CHECKLIST", ascii_table(["Setting", "Recommended"], cl_rows), ""]
    txt_text = "\n".join(txt) + "\n"

    color = {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#cf222e",
             "INFO": "#57606a"}.get(rec["verdict"], "#57606a")
    html = ["<html><body style='font-family:sans-serif;font-size:14px'>",
            "<h2>AMD platform tuning report</h2>",
            f"<p><b>Verdict:</b> <span style='color:{color}'>{rec['verdict']}</span></p>",
            f"<p><b>Host / SoC:</b> {rec['host']} / {rec['family']}<br>"
            f"<b>CPU:</b> {rec['model']}<br>"
            f"<b>Generated (UTC):</b> {rec['generated']}</p>",
            "<h3>Checks</h3>", html_table(chk_hdr, chk_rows)]
    if rec.get("redfish"):
        rf_rows = [[r["attribute"], r["expected"], r["observed"], r["status"]]
                   for r in rec["redfish"]]
        html += ["<h3>BIOS via Redfish</h3>",
                 html_table(["Attribute", "Expected", "Observed", "Status"], rf_rows)]
    if remed:
        html += ["<h3>Remediations</h3><ul>"] + [f"<li>{r}</li>" for r in remed] + ["</ul>"]
    html += ["<h3>BIOS manual checklist</h3>",
             html_table(["Setting", "Recommended"], cl_rows),
             "</body></html>"]
    html_text = "\n".join(html)
    return md_text, txt_text, html_text


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="AMD platform tuning check")
    ap.add_argument("--host", help="override DUT host (default: DUT_HOST)")
    ap.add_argument("--apply-grub", action="store_true",
                    help="rewrite GRUB_CMDLINE_LINUX with the recommended line")
    ap.add_argument("--reboot", action="store_true",
                    help="reboot the host and wait for it to return, then re-check")
    ap.add_argument("--bios-redfish", action="store_true",
                    help="query BMC Redfish BIOS attributes (needs DUT_BMC_IP)")
    ap.add_argument("--pcie", help="comma-separated PCI BDFs to link-check")
    args = ap.parse_args()

    cfg = load_config()
    tuning = store.load_tuning()
    if not tuning:
        log("no config/amd-tuning.json(.example) found")
        return 1

    host = args.host or cfg.get("DUT_HOST", "")
    if not host:
        log("no DUT_HOST set - run: python scripts/accel.py discover <DUT_IP>")
        return 1
    user = cfg["SSH_USER"]
    pw = ssh_pass(cfg, host)

    # PCIe BDFs: --pcie, else TUNE_PCIE_BDFS, else any accelerator bdf.
    bdfs: list[str] = []
    if args.pcie:
        bdfs = [b.strip() for b in args.pcie.split(",") if b.strip()]
    elif cfg.get("TUNE_PCIE_BDFS"):
        bdfs = [b.strip() for b in cfg["TUNE_PCIE_BDFS"].split(",") if b.strip()]
    else:
        for a in store.load_accelerators().values():
            if a.get("bdf"):
                bdfs.append(a["bdf"])

    def run_check() -> dict:
        _, raw = run_remote_script(host, user, pw, _probe_script(bdfs), 90)
        observed = parse_probe(raw)
        family = detect_family(observed["model"], tuning, cfg.get("CPU_SOC", ""))
        if not family:
            log(f"could not detect SoC family from '{observed['model']}'; "
                "set CPU_SOC in config/lab.hosts")
            family = "turin"
        profile = tuning["families"].get(family, {})
        checks = evaluate(profile, observed, cfg)
        rec = {
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
        if args.bios_redfish:
            rec["redfish"] = redfish_bios(cfg, profile, tuning)
        return rec

    log(f"=== platform tuning check: {host} ===")
    rec = run_check()

    if args.apply_grub:
        profile = tuning["families"].get(rec["family"], {})
        line = recommended_grub_line(profile, cfg)
        if not line:
            log("  no apply_base in profile; cannot apply GRUB")
        else:
            need_reboot = apply_grub(host, user, pw, line)
            if args.reboot:
                log(f"  rebooting {host} to activate tuning")
                reboot_host(host, user, pw)
                if wait_for_ssh(host, user, pw):
                    log("  re-checking after reboot")
                    rec = run_check()
                else:
                    log(f"  {host} did not return within timeout")
            elif need_reboot:
                log("  REBOOT REQUIRED to activate tuning (re-run with --reboot)")
    elif args.reboot:
        log(f"  rebooting {host}")
        reboot_host(host, user, pw)
        if wait_for_ssh(host, user, pw):
            rec = run_check()

    # Persist record + reports.
    TUNING_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = store.now_ts()
    safe_host = re.sub(r"[^\w.-]", "_", host)
    json_path = TUNING_DIR / f"{safe_host}_{ts}.json"
    json_path.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")

    md_text, txt_text, html_text = render_report(rec, tuning)
    md_path = REPORTS / f"tuning_{ts}.md"
    txt_path = REPORTS / f"tuning_{ts}.txt"
    html_path = REPORTS / f"tuning_{ts}.html"
    md_path.write_text(md_text, encoding="utf-8")
    txt_path.write_text(txt_text, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    (RESULTS / ".latest_tuning").write_text(str(json_path) + "\n", encoding="utf-8")
    (REPORTS / ".latest_tuning").write_text(
        f"{md_path}\n{txt_path}\n{html_path}\n{rec['verdict']}\n", encoding="utf-8")

    print()
    print(txt_text)
    log(f"Tuning report:  {md_path}")
    log(f"Verdict:        {rec['verdict']}")
    return 0 if rec["verdict"] != "FAIL" else 2


if __name__ == "__main__":
    sys.exit(main())
