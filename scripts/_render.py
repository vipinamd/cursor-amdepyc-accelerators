#!/usr/bin/env python3
"""Shared table renderers for ASCII (email body), HTML, and Markdown."""
from __future__ import annotations


def ascii_table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    line = lambda r: "|" + "|".join(f" {r[i]:<{widths[i]}} " for i in range(len(headers))) + "|"
    out = [sep, line(headers), sep]
    for row in cells:
        out.append(line(row))
    out.append(sep)
    return "\n".join(out)


def md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def html_table(headers: list[str], rows: list[list]) -> str:
    th = "".join(f"<th style='padding:6px;border:1px solid #ccc'>{h}</th>" for h in headers)
    body = []
    for row in rows:
        tds = "".join(f"<td style='padding:6px;border:1px solid #ccc'>{c}</td>" for c in row)
        body.append(f"<tr>{tds}</tr>")
    return ("<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
            f"<thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>")
