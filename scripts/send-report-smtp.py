#!/usr/bin/env python3
"""Send the latest executive summary by email via direct SMTP.

Reads recipients/relay from config/report-email.conf (see report-email.example).
Defaults to DRY_RUN=1: prints the message instead of sending. Sends the TXT
body with the HTML summary as an alternative part so it renders in clients.
"""
from __future__ import annotations

import argparse
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _lab_common import REPORTS, load_config, log


def _latest_summary() -> tuple[Path | None, Path | None, str]:
    marker = REPORTS / ".latest_summary"
    if not marker.exists():
        return None, None, "UNKNOWN"
    lines = marker.read_text(encoding="utf-8").splitlines()
    md = Path(lines[0]) if len(lines) > 0 else None
    txt = Path(lines[1]) if len(lines) > 1 else None
    html = Path(lines[2]) if len(lines) > 2 else None
    verdict = lines[3] if len(lines) > 3 else "UNKNOWN"
    return txt, html, verdict


def _latest_comparison() -> tuple[Path | None, Path | None, str]:
    marker = REPORTS / ".latest_comparison"
    if not marker.exists():
        return None, None, "comparison"
    lines = marker.read_text(encoding="utf-8").splitlines()
    # marker holds md, txt, html, csv
    txt = Path(lines[1]) if len(lines) > 1 else None
    html = Path(lines[2]) if len(lines) > 2 else None
    return txt, html, "comparison"


def main() -> int:
    ap = argparse.ArgumentParser(description="email the latest summary or comparison")
    ap.add_argument("--to", help="override recipient(s), comma-separated")
    ap.add_argument("--cc", help="override Cc; pass empty string to drop Cc")
    ap.add_argument("--subject", help="override the subject line")
    ap.add_argument("--comparison", action="store_true",
                    help="send the latest cross-run comparison instead of the run summary")
    args = ap.parse_args()

    cfg = load_config()
    to = args.to if args.to is not None else cfg.get("TO", "")
    cc = args.cc if args.cc is not None else cfg.get("CC", "")
    sender = cfg.get("FROM", "")
    dry = cfg.get("DRY_RUN", "1") != "0"

    if args.comparison:
        txt_path, html_path, verdict = _latest_comparison()
        default_subject = "[accel-bench] topology placement comparison"
    else:
        txt_path, html_path, verdict = _latest_summary()
        default_subject = f"[accel-bench] run summary - verdict {verdict}"
    if not txt_path or not txt_path.exists():
        log("no report found; run analyze-accel-run.py --latest or compare-accel-runs.py first")
        return 1
    body = txt_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8") if html_path and html_path.exists() else None

    msg = EmailMessage()
    msg["Subject"] = args.subject or default_subject
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    if dry or not to:
        log("DRY_RUN (or no TO set): not sending. Message preview:")
        print(f"Subject: {msg['Subject']}")
        print(f"To: {to}\nCc: {cc}\nFrom: {sender}\n")
        print(body)
        return 0

    server = cfg.get("SMTP_SERVER", "")
    port = int(cfg.get("SMTP_PORT", "25"))
    log(f"sending to {to} via {server}:{port}")
    with smtplib.SMTP(server, port, timeout=30) as s:
        if cfg.get("SMTP_TLS", "1") == "1":
            s.starttls()
        if cfg.get("SMTP_USER"):
            s.login(cfg["SMTP_USER"], cfg.get("SMTP_PASS", ""))
        s.send_message(msg)
    log("sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
