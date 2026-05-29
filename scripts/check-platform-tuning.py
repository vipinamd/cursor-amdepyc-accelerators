#!/usr/bin/env python3
"""Check an AMD EPYC DUT against the DPDK AMD platform tuning guide.

Verifies GRUB kernel parameters, OS-observable BIOS indicators (SMT, NPS,
C-states, boost, IOMMU, x2APIC, governor), power setup (amd_pstate, HSMP),
system config (kernel, THP, 1G hugepages, tuned profile), and PCIe link speed
against the per-family profile in config/amd-tuning.json (see
https://doc.dpdk.org/guides/linux_gsg/amd_platform.html). Produces a
PASS/WARN/FAIL tuning report (JSON + MD/TXT/HTML).

The probe + evaluation live in _tuning.py and are shared with the benchmark
runner (which captures a snapshot into every run record).

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
import _tuning as tuning_lib
from _lab_common import (
    REPORTS, RESULTS, load_config, log, reboot_host, run_remote_script,
    ssh_pass, wait_for_ssh,
)
from _render import ascii_table, html_table, md_table

TUNING_DIR = RESULTS / "tuning"


# --------------------------------------------------------------------------
# optional: Redfish BIOS (GRUB apply/reboot helpers live in _tuning)
# --------------------------------------------------------------------------
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
        rec = tuning_lib.snapshot(cfg, tuning, host, user, pw, bdfs)
        if rec["family"] not in tuning.get("families", {}):
            log(f"could not detect SoC family from '{rec['model']}'; "
                "set CPU_SOC in config/lab.hosts")
        if args.bios_redfish:
            profile = tuning["families"].get(rec["family"], {})
            rec["redfish"] = redfish_bios(cfg, profile, tuning)
        return rec

    log(f"=== platform tuning check: {host} ===")
    rec = run_check()

    if args.apply_grub:
        profile = tuning["families"].get(rec["family"], {})
        line = tuning_lib.recommended_grub_line(profile, cfg)
        if not line:
            log("  no apply_base in profile; cannot apply GRUB")
        else:
            need_reboot = tuning_lib.apply_grub(host, user, pw, line)
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
