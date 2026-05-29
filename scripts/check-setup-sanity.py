#!/usr/bin/env python3
"""Setup sanity preflight for an accelerator benchmark.

Checks compiler/toolchain, the DPDK build, free hugepages, device binding, and
configuration, and rolls up BIOS/GRUB from the platform tuning snapshot. Reports
PASS/WARN/FAIL with a `blocker` flag (a blocker makes the test impossible).

Read-only by default. With --fix it self-heals (install toolchain, build DPDK,
apply GRUB + reboot, reserve hugepages, bind vfio-pci), then re-runs the checks.

Usage:
  python check-setup-sanity.py                 # check the first enabled accel
  python check-setup-sanity.py --accel cpu_dma
  python check-setup-sanity.py --accel dsa --fix
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
import _sanity as sanity_lib
import _tuning as tuning_lib
from _lab_common import (
    REPORTS, RESULTS, load_config, log, reboot_host, ssh_pass, wait_for_ssh,
)
from _render import ascii_table, md_table

SANITY_DIR = RESULTS / "sanity"
_COLOR = {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#cf222e", "INFO": "#57606a"}


def _pick_accel(accelerators: dict, name: str | None) -> tuple[str, dict]:
    if name:
        if name not in accelerators:
            raise SystemExit(f"unknown accelerator '{name}'")
        return name, accelerators[name]
    for k, v in accelerators.items():
        if v.get("enabled"):
            return k, v
    raise SystemExit("no enabled accelerator in config/accelerators.json")


def _sanity_html_table(rows: list[dict]) -> str:
    hdr = ["Category", "Item", "Expected", "Observed", "Status"]
    th = "".join(f"<th style='padding:6px;border:1px solid #ccc'>{h}</th>" for h in hdr)
    body = []
    for r in rows:
        col = _COLOR.get(r["status"], "#57606a")
        weight = "bold" if r["status"] in ("WARN", "FAIL") else "normal"
        tag = " (blocker)" if r["blocker"] and r["status"] == "FAIL" else ""
        tds = "".join(f"<td style='padding:6px;border:1px solid #ccc'>{v}</td>"
                      for v in (r["category"], r["item"], r["expected"], r["observed"]))
        tds += (f"<td style='padding:6px;border:1px solid #ccc;color:{col};"
                f"font-weight:{weight}'>{r['status']}{tag}</td>")
        body.append(f"<tr>{tds}</tr>")
    return ("<table style='border-collapse:collapse;font-family:sans-serif;"
            f"font-size:14px'><thead><tr>{th}</tr></thead><tbody>"
            f"{''.join(body)}</tbody></table>")


def render_report(rec: dict) -> tuple[str, str, str]:
    hdr = ["Category", "Item", "Expected", "Observed", "Status"]
    rows = [[r["category"], r["item"], r["expected"], r["observed"],
             r["status"] + (" (blocker)" if r["blocker"] and r["status"] == "FAIL" else "")]
            for r in rec["rows"]]
    remed = [r["remediation"] for r in rec["rows"]
             if r["status"] != "PASS" and r["remediation"]]
    nblock = sanity_lib.blocker_count(rec["rows"])
    line = f"{rec['verdict']} - {nblock} blocker(s), {sanity_lib.diff_count(rec['rows'])} issue(s)"

    md = ["# Setup sanity report", "",
          f"- **Verdict:** {line}",
          f"- **Host / accel:** {rec['host']} / {rec['accelerator']} ({rec['tool']})",
          f"- **Generated (UTC):** {rec['generated']}", "",
          "## Checks", "", md_table(hdr, rows), ""]
    if rec.get("remediated"):
        md += ["## Applied remediations", ""] + [f"- {r}" for r in rec["remediated"]] + [""]
    if remed:
        md += ["## Remediations", ""] + [f"- {r}" for r in remed] + [""]
    md_text = "\n".join(md) + "\n"

    txt = ["Setup sanity report", "=" * 36, "",
           f"Verdict: {line}",
           f"Host/accel: {rec['host']} / {rec['accelerator']} ({rec['tool']})", "",
           "CHECKS", ascii_table(hdr, rows), ""]
    if rec.get("remediated"):
        txt += ["APPLIED REMEDIATIONS"] + [f"  - {r}" for r in rec["remediated"]] + [""]
    if remed:
        txt += ["REMEDIATIONS"] + [f"  - {r}" for r in remed] + [""]
    txt_text = "\n".join(txt) + "\n"

    color = _COLOR.get(rec["verdict"], "#57606a")
    html = ["<html><body style='font-family:sans-serif;font-size:14px'>",
            "<h2>Setup sanity report</h2>",
            f"<p><b>Verdict:</b> <span style='color:{color}'>{line}</span></p>",
            f"<p><b>Host / accel:</b> {rec['host']} / {rec['accelerator']} "
            f"({rec['tool']})<br><b>Generated (UTC):</b> {rec['generated']}</p>",
            "<h3>Checks</h3>", _sanity_html_table(rec["rows"])]
    if rec.get("remediated"):
        html += ["<h3>Applied remediations</h3><ul>"] + \
                [f"<li>{r}</li>" for r in rec["remediated"]] + ["</ul>"]
    if remed:
        html += ["<h3>Remediations</h3><ul>"] + [f"<li>{r}</li>" for r in remed] + ["</ul>"]
    html += ["</body></html>"]
    return md_text, txt_text, "\n".join(html)


def main() -> int:
    ap = argparse.ArgumentParser(description="setup sanity preflight")
    ap.add_argument("--accel", help="accelerator name (default: first enabled)")
    ap.add_argument("--host", help="override DUT host (default: DUT_HOST)")
    ap.add_argument("--fix", action="store_true",
                    help="remediate shortcomings (install/build/GRUB/hugepages/bind), reboot if needed, re-check")
    args = ap.parse_args()

    cfg = load_config()
    tuning_cfg = store.load_tuning()
    sanity_cfg = store.load_sanity()
    accelerators = store.load_accelerators()
    accel, accel_cfg = _pick_accel(accelerators, args.accel)
    tool = accel_cfg.get("tool", "")

    host = args.host or cfg.get("DUT_HOST", "")
    if not host:
        log("no DUT_HOST set - run: python scripts/accel.py discover <DUT_IP>")
        return 1
    user = cfg["SSH_USER"]
    pw = ssh_pass(cfg, host)

    def run_check() -> dict:
        tsnap = {}
        try:
            tsnap = tuning_lib.snapshot(cfg, tuning_cfg, host, user, pw, [])
        except Exception as exc:  # noqa: BLE001 - tuning rollup is best-effort
            log(f"  tuning snapshot unavailable ({type(exc).__name__})")
        return sanity_lib.snapshot(cfg, sanity_cfg, accel, accel_cfg, tool,
                                   host, user, pw, tsnap)

    log(f"=== setup sanity: {host} / {accel} ({tool}) ===")
    rec = run_check()

    if args.fix and (rec["blocker"] or sanity_lib.diff_count(rec["rows"])):
        tsnap = {}
        try:
            tsnap = tuning_lib.snapshot(cfg, tuning_cfg, host, user, pw, [])
        except Exception:  # noqa: BLE001
            pass
        applied, reboot_needed = sanity_lib.remediate(
            rec["rows"], cfg, accel_cfg, tsnap, tuning_cfg, host, user, pw)
        rec["remediated"] = applied
        if reboot_needed:
            log(f"  rebooting {host} to activate GRUB changes")
            reboot_host(host, user, pw)
            if not wait_for_ssh(host, user, pw):
                log(f"  {host} did not return within timeout")
        log("  re-running sanity after remediation")
        recheck = run_check()
        recheck["remediated"] = applied
        recheck["rebooted"] = reboot_needed
        rec = recheck

    # Persist record + reports.
    SANITY_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = store.now_ts()
    safe = re.sub(r"[^\w.-]", "_", f"{host}_{accel}")
    json_path = SANITY_DIR / f"{safe}_{ts}.json"
    json_path.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")

    md_text, txt_text, html_text = render_report(rec)
    (REPORTS / f"sanity_{ts}.md").write_text(md_text, encoding="utf-8")
    (REPORTS / f"sanity_{ts}.txt").write_text(txt_text, encoding="utf-8")
    (REPORTS / f"sanity_{ts}.html").write_text(html_text, encoding="utf-8")
    (RESULTS / ".latest_sanity").write_text(str(json_path) + "\n", encoding="utf-8")

    print()
    print(txt_text)
    log(f"Sanity report:  {REPORTS / f'sanity_{ts}.md'}")
    log(f"Verdict:        {rec['verdict']} (blockers: {sanity_lib.blocker_count(rec['rows'])})")
    return 2 if rec["blocker"] else 0


if __name__ == "__main__":
    sys.exit(main())
