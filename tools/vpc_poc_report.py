#!/usr/bin/env python3
"""
vpc_poc_report.py — Generate an HTML activity report from vpc_poc_setup.log.

Usage:
    python3 tools/vpc_poc_report.py
    python3 tools/vpc_poc_report.py --log vpc_poc_setup.log --out vpc_poc_report.html
    python3 tools/vpc_poc_report.py --open   # auto-open in browser after generating

Log format expected (produced by vpc_poc_setup.py):
    2026-11-20 10:30:00  INFO      _ensure_vpc                          VPC created  id=vpc-xxx
"""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── Log line parser ────────────────────────────────────────────────────────────

LOG_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"\s{2}(?P<level>[A-Z]+)\s+"
    r"(?P<func>\S+)\s+"
    r"(?P<msg>.*)$"
)

LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


@dataclass
class LogEntry:
    ts: str
    level: str
    func: str
    msg: str
    raw: str
    continuation: bool = False   # True for stack-trace / continuation lines


def parse_log(path: str) -> list[LogEntry]:
    entries: list[LogEntry] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            m = LOG_PATTERN.match(raw)
            if m:
                entries.append(LogEntry(
                    ts=m.group("ts"),
                    level=m.group("level").strip(),
                    func=m.group("func"),
                    msg=m.group("msg"),
                    raw=raw,
                ))
            else:
                # Stack trace or continuation line — attach to last entry visually
                entries.append(LogEntry(
                    ts="", level="DEBUG", func="", msg=raw, raw=raw,
                    continuation=True,
                ))
    return entries


# ── Run detection ──────────────────────────────────────────────────────────────

def split_into_runs(entries: list[LogEntry]) -> list[list[LogEntry]]:
    """Split the log into separate runs separated by the '===…===' banner."""
    runs: list[list[LogEntry]] = []
    current: list[LogEntry] = []
    for e in entries:
        if not e.continuation and "=" * 20 in e.msg and not current:
            current = [e]
        elif not e.continuation and "=" * 20 in e.msg and current:
            # Second banner line often closes a run; start a new run on next non-banner
            current.append(e)
        else:
            if current is not None:
                current.append(e)
            else:
                current = [e]
    if current:
        runs.append(current)
    return runs if runs else [entries]


def run_meta(run: list[LogEntry]) -> dict:
    """Extract action, region, timestamp from the banner lines of a run."""
    meta = {"action": "?", "region": "?", "started": "?", "ended": "?"}
    for e in run:
        if "vpc_poc_setup.py started" in e.msg:
            m = re.search(r"action=(\S+)", e.msg)
            if m:
                meta["action"] = m.group(1)
            m = re.search(r"region=(\S+)", e.msg)
            if m:
                meta["region"] = m.group(1)
            meta["started"] = e.ts
        if e.ts:
            meta["ended"] = e.ts
    return meta


def count_levels(run: list[LogEntry]) -> dict[str, int]:
    counts: dict[str, int] = {k: 0 for k in LEVEL_ORDER}
    for e in run:
        if not e.continuation and e.level in counts:
            counts[e.level] += 1
    return counts


# ── HTML helpers ───────────────────────────────────────────────────────────────

LEVEL_CSS = {
    "DEBUG":    "lvl-debug",
    "INFO":     "lvl-info",
    "WARNING":  "lvl-warn",
    "ERROR":    "lvl-error",
    "CRITICAL": "lvl-critical",
}


def _h(text: str) -> str:
    return html.escape(str(text))


def _badge(level: str, count: int) -> str:
    cls = LEVEL_CSS.get(level, "lvl-debug")
    return f'<span class="badge {cls}">{level} <strong>{count}</strong></span>'


def _row(e: LogEntry) -> str:
    if e.continuation:
        return (
            f'<tr class="continuation">'
            f'<td colspan="4"><code>{_h(e.msg)}</code></td>'
            f'</tr>'
        )
    cls = LEVEL_CSS.get(e.level, "lvl-debug")
    return (
        f'<tr class="{cls}">'
        f'<td class="ts">{_h(e.ts)}</td>'
        f'<td class="level"><span class="badge {cls}">{_h(e.level)}</span></td>'
        f'<td class="func"><code>{_h(e.func)}</code></td>'
        f'<td class="msg">{_h(e.msg)}</td>'
        f'</tr>'
    )


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: "Segoe UI", system-ui, sans-serif;
    background: #0f1117;
    color: #d1d5db;
    padding: 24px;
    font-size: 13px;
}
h1 { font-size: 1.4rem; color: #f9fafb; margin-bottom: 4px; }
.subtitle { color: #6b7280; margin-bottom: 24px; font-size: 0.85rem; }
.run-card {
    background: #1a1d27;
    border: 1px solid #2d3148;
    border-radius: 10px;
    margin-bottom: 32px;
    overflow: hidden;
}
.run-header {
    background: #21253a;
    padding: 14px 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    border-bottom: 1px solid #2d3148;
}
.run-header h2 { font-size: 1rem; color: #f9fafb; }
.run-meta { font-size: 0.78rem; color: #9ca3af; }
.badges { display: flex; gap: 6px; flex-wrap: wrap; margin-left: auto; }
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 600;
    white-space: nowrap;
}
.lvl-debug    { background: #1e293b; color: #94a3b8; }
.lvl-info     { background: #0f2d1f; color: #34d399; }
.lvl-warn     { background: #2d1f00; color: #fbbf24; }
.lvl-error    { background: #2d0f0f; color: #f87171; }
.lvl-critical { background: #4c0519; color: #fb7185; }

.filter-bar {
    display: flex; gap: 8px; padding: 10px 20px;
    background: #181b28; border-bottom: 1px solid #2d3148; flex-wrap: wrap;
}
.filter-btn {
    padding: 3px 10px; border-radius: 4px; border: 1px solid #374151;
    cursor: pointer; font-size: 0.75rem; background: #0f1117; color: #9ca3af;
    transition: all 0.15s;
}
.filter-btn.active { border-color: #6366f1; color: #a5b4fc; background: #1e1b4b; }
.filter-btn:hover { border-color: #6b7280; }

.search-box {
    padding: 3px 10px; border-radius: 4px; border: 1px solid #374151;
    background: #0f1117; color: #d1d5db; font-size: 0.78rem; min-width: 220px;
}
.search-box:focus { outline: none; border-color: #6366f1; }

table { width: 100%; border-collapse: collapse; }
thead th {
    background: #161929; color: #6b7280; font-weight: 600;
    padding: 8px 12px; text-align: left; font-size: 0.72rem;
    text-transform: uppercase; letter-spacing: 0.05em;
    position: sticky; top: 0; z-index: 1;
}
tbody tr { border-bottom: 1px solid #1f2435; }
tbody tr:hover td { background: #1e2235 !important; }
td { padding: 5px 12px; vertical-align: top; line-height: 1.5; }
td.ts   { white-space: nowrap; color: #6b7280; font-family: monospace; width: 150px; }
td.level { width: 90px; }
td.func { width: 220px; }
td.msg  { font-family: monospace; word-break: break-all; }
tr.continuation td { background: #12151f; color: #4b5563; padding-left: 24px; }

tr.lvl-debug td   { background: #12151f; }
tr.lvl-info td    { }
tr.lvl-warn td    { background: #1c1500; }
tr.lvl-error td   { background: #1c0a0a; }
tr.lvl-critical td { background: #2d0416; }

.summary-bar {
    display: flex; gap: 24px; padding: 12px 20px;
    background: #12151f; border-top: 1px solid #2d3148;
    font-size: 0.78rem; color: #6b7280; flex-wrap: wrap;
}
.summary-bar span strong { color: #d1d5db; }

.table-wrap { overflow-x: auto; max-height: 600px; overflow-y: auto; }

.no-rows { padding: 20px; text-align: center; color: #4b5563; font-size: 0.85rem; }
"""

JS = """
function filterTable(tableId, level) {
    const table = document.getElementById(tableId);
    const btns = table.closest('.run-card').querySelectorAll('.filter-btn');
    btns.forEach(b => b.classList.toggle('active', b.dataset.level === level || (level === 'ALL' && b.dataset.level === 'ALL')));

    const rows = table.querySelectorAll('tbody tr');
    const RANK = {DEBUG:0, INFO:1, WARNING:2, ERROR:3, CRITICAL:4};
    const minRank = level === 'ALL' ? -1 : (RANK[level] ?? -1);
    rows.forEach(row => {
        if (row.classList.contains('continuation')) {
            row.style.display = '';
            return;
        }
        const badge = row.querySelector('.badge');
        const rowLevel = badge ? badge.textContent.trim() : 'DEBUG';
        const rowRank = RANK[rowLevel] ?? 0;
        row.style.display = (minRank === -1 || rowRank >= minRank) ? '' : 'none';
    });
    checkEmpty(table);
}

function searchTable(tableId, query) {
    const table = document.getElementById(tableId);
    const rows = table.querySelectorAll('tbody tr');
    const q = query.toLowerCase();
    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = (!q || text.includes(q)) ? '' : 'none';
    });
    checkEmpty(table);
}

function checkEmpty(table) {
    const visible = [...table.querySelectorAll('tbody tr')].filter(r => r.style.display !== 'none');
    let noRows = table.closest('.table-wrap').querySelector('.no-rows');
    if (!noRows) {
        noRows = document.createElement('div');
        noRows.className = 'no-rows';
        noRows.textContent = 'No matching log entries.';
        table.closest('.table-wrap').appendChild(noRows);
    }
    noRows.style.display = visible.length === 0 ? '' : 'none';
}
"""


def build_html(runs: list[list[LogEntry]], log_path: str) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    run_blocks: list[str] = []
    for idx, run in enumerate(runs):
        meta   = run_meta(run)
        counts = count_levels(run)
        table_id = f"tbl-{idx}"
        run_idx  = idx + 1

        # Header badges
        badges = "".join(
            _badge(lvl, counts[lvl])
            for lvl in ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
            if counts[lvl] > 0
        )

        # Filter buttons
        filter_btns = (
            f'<button class="filter-btn active" data-level="ALL" '
            f'onclick="filterTable(\'{table_id}\',\'ALL\')">ALL</button>'
        )
        for lvl in ["INFO", "WARNING", "ERROR", "CRITICAL"]:
            filter_btns += (
                f'<button class="filter-btn" data-level="{lvl}" '
                f'onclick="filterTable(\'{table_id}\',\'{lvl}\')">{lvl}+</button>'
            )

        # Table rows
        rows_html = "\n".join(_row(e) for e in run)

        total = sum(counts.values())
        summary = (
            f'<span>Total: <strong>{total}</strong></span>'
            + "".join(
                f'<span>{lvl}: <strong>{counts[lvl]}</strong></span>'
                for lvl in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            )
        )

        run_blocks.append(f"""
<div class="run-card">
  <div class="run-header">
    <div>
      <h2>Run #{run_idx} — {_h(meta["action"])}</h2>
      <div class="run-meta">
        Region: {_h(meta["region"])} &nbsp;|&nbsp;
        Started: {_h(meta["started"])} &nbsp;|&nbsp;
        Last entry: {_h(meta["ended"])}
      </div>
    </div>
    <div class="badges">{badges}</div>
  </div>
  <div class="filter-bar">
    {filter_btns}
    <input class="search-box" type="text" placeholder="Search log…"
           oninput="searchTable('{table_id}', this.value)">
  </div>
  <div class="table-wrap">
    <table id="{table_id}">
      <thead>
        <tr>
          <th>Timestamp</th>
          <th>Level</th>
          <th>Function</th>
          <th>Message</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
  <div class="summary-bar">{summary}</div>
</div>""")

    all_runs_html = "\n".join(run_blocks)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VPC POC Activity Report</title>
  <style>{CSS}</style>
</head>
<body>
  <h1>VPC POC Activity Report</h1>
  <p class="subtitle">
    Source: <code>{_h(log_path)}</code> &nbsp;|&nbsp;
    Generated: {_h(generated_at)} &nbsp;|&nbsp;
    {len(runs)} run(s)
  </p>
  {all_runs_html}
  <script>{JS}</script>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--log", default="vpc_poc_setup.log",
        help="Path to the log file (default: vpc_poc_setup.log)",
    )
    parser.add_argument(
        "--out", default="vpc_poc_report.html",
        help="Output HTML file path (default: vpc_poc_report.html)",
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open the report in the default browser after generating",
    )
    args = parser.parse_args()

    log_path = args.log
    if not os.path.exists(log_path):
        print(f"[ERROR] Log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    print(f"  Parsing  {log_path} …")
    entries = parse_log(log_path)
    runs    = split_into_runs(entries)
    print(f"  Found    {len(entries)} log lines across {len(runs)} run(s)")

    html_content = build_html(runs, log_path)

    out_path = args.out
    Path(out_path).write_text(html_content, encoding="utf-8")
    abs_out = os.path.abspath(out_path)
    print(f"  Report   {abs_out}")

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{abs_out}")
        print("  Opened in browser.")


if __name__ == "__main__":
    main()
