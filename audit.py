#!/usr/bin/env python3
"""
Audit Vitamix pages for proper trademark symbol usage (URL-list prototype, text marks only).

Usage:
  python audit.py \
    --urls-file top_urls_clean_us_en.csv \
    --marks-file trademarks_us_2025_text_only.json \
    --out runs/out \
    --concurrency 4 --rps 2 \
    --save-flagged-screenshots

Inputs:
- CSV with a column named "url".
- Marks JSON:
  {
    "marks": [
      {
        "term": "Vitamix",
        "symbol": "®",
        "variants": [],
        "case_insensitive": true,
        "policy": "first_prominent_only",
        "locales": ["en-US"]
      }
    ]
  }

Outputs in --out:
- findings.csv
- findings.jsonl
- report.html
- (optional) screenshots for flagged pages
"""

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd
from playwright.async_api import async_playwright, Browser, Page


# -----------------------------
# Input loaders
# -----------------------------
def read_urls(csv_path: str) -> List[str]:
    """Read a list of URLs from a CSV file with column 'url'."""
    urls: List[str] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "url" not in (reader.fieldnames or []):
            raise ValueError(f"CSV file {csv_path} must contain a 'url' column")
        for row in reader:
            u = (row.get("url") or "").strip()
            if u:
                urls.append(u)
    return urls


def read_marks(json_path: str) -> List[Dict[str, Any]]:
    """Load mark definitions from JSON file and normalize."""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    marks: List[Dict[str, Any]] = []
    for m in data.get("marks", []):
        term = str(m.get("term", "")).strip()
        if not term:
            continue
        marks.append(
            {
                "term": term,
                "symbol": str(m.get("symbol", "")).strip(),
                "variants": m.get("variants", []) or [],
                "case_insensitive": bool(m.get("case_insensitive", True)),
                "policy": m.get("policy", "first_prominent_only"),
                "locales": m.get("locales", []) or ["en-US"],
            }
        )
    return marks


# -----------------------------
# DOM evaluation helpers
# -----------------------------
async def evaluate_prominent_elements(page: Page) -> List[Dict[str, str]]:
    """
    Return candidate nodes for prominent text as dicts:
    {text, path, html}. Order implies priority.
    """
    script = """
    () => {
      function isVisible(el) {
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = el.getBoundingClientRect();
        return (rect.width > 0 && rect.height > 0);
      }
      const root = document.querySelector('main') || document.body;

      const candidates = [];
      const addAll = (selector) => {
        root.querySelectorAll(selector).forEach(el => { if (isVisible(el)) candidates.push(el); });
      };

      // Prefer headings, then hero/title classes, then early body copy.
      ['h1','h2','h3'].forEach(sel => addAll(sel));
      ['.hero','.product-title','.page-intro','.pdp-title','.tile-title'].forEach(sel => addAll(sel));

      // First visible <p> or <li> in main
      const textNodes = Array.from(root.querySelectorAll('p, li'));
      for (const el of textNodes) {
        if (isVisible(el)) { candidates.push(el); break; }
      }

      const buildPath = (el) => {
        const parts = [];
        let node = el;
        while (node && node.nodeType === Node.ELEMENT_NODE) {
          let part = node.tagName.toLowerCase();
          if (node.id) part += '#' + node.id;
          if (node.classList && node.classList.length) part += '.' + Array.from(node.classList).join('.');
          parts.unshift(part);
          node = node.parentElement;
        }
        return parts.join(' > ');
      };

      return candidates.map(el => ({
        text: el.textContent || '',
        path: buildPath(el),
        html: el.innerHTML || ''
      }));
    }
    """
    return await page.evaluate(script)


def find_first_term(text: str, term: str, case_insensitive: bool) -> int:
    """Return index of first occurrence of term (or -1)."""
    return (text.lower().find(term.lower()) if case_insensitive else text.find(term))


# -----------------------------
# Page processing
# -----------------------------
async def process_url(
    page: Page,
    url: str,
    marks: List[Dict[str, Any]],
    save_screenshot: bool,
    out_dir: str,
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    try:
        # Faster, less-stally than "networkidle"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    except Exception as exc:
        findings.append(
            {
                "url": url,
                "term": "",
                "issue": "navigation error",
                "expected": "",
                "found": "",
                "path": "",
                "snippet": "",
                "details": str(exc),
            }
        )
        return findings

    try:
        candidates = await evaluate_prominent_elements(page)
    except Exception as exc:
        findings.append(
            {
                "url": url,
                "term": "",
                "issue": "evaluation error",
                "expected": "",
                "found": "",
                "path": "",
                "snippet": "",
                "details": str(exc),
            }
        )
        return findings

    page_has_issue = False

    for mark in marks:
        term = mark["term"]
        symbol = mark["symbol"]
        ci = mark.get("case_insensitive", True)

        issue_recorded = False
        for candidate in candidates:
            text = candidate["text"] or ""
            idx = find_first_term(text, term, ci)
            if idx == -1:
                continue

            # Found term in this candidate; check the following char (after optional spaces/punct)
            end = idx + len(term)
            actual_symbol = ""
            j = end
            while j < len(text) and text[j] in " \t\r\n\u00A0.-–—:,":
                j += 1
            if j < len(text):
                actual_symbol = text[j]

            if actual_symbol == symbol:
                # Correct symbol at first prominent occurrence
                pass
            elif actual_symbol in ["®", "™"] and actual_symbol != symbol:
                findings.append(
                    {
                        "url": url,
                        "term": term,
                        "issue": "wrong symbol",
                        "expected": symbol,
                        "found": actual_symbol,
                        "path": candidate["path"],
                        "snippet": (text.strip()[:300]),
                        "details": "",
                    }
                )
                issue_recorded = True
                page_has_issue = True
            else:
                findings.append(
                    {
                        "url": url,
                        "term": term,
                        "issue": "missing symbol",
                        "expected": symbol,
                        "found": actual_symbol or "",
                        "path": candidate["path"],
                        "snippet": (text.strip()[:300]),
                        "details": "",
                    }
                )
                issue_recorded = True
                page_has_issue = True

            break  # only the first prominent occurrence per term

        if not issue_recorded:
            findings.append(
                {
                    "url": url,
                    "term": term,
                    "issue": "not found in prominent text",
                    "expected": symbol,
                    "found": "",
                    "path": "",
                    "snippet": "",
                    "details": "",
                }
            )

    if save_screenshot and page_has_issue:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        safe = url.replace("://", "__").replace("/", "_")
        filepath = os.path.join(out_dir, f"{safe}_{ts}.png")
        try:
            await page.screenshot(path=filepath, full_page=True)
        except Exception:
            # best-effort; ignore screenshot errors
            pass

    return findings


# -----------------------------
# Runner with per-worker pages + progress
# -----------------------------
async def run_audit(
    urls: List[str],
    marks: List[Dict[str, Any]],
    out_dir: str,
    concurrency: int,
    rps: float,
    save_screenshots: bool,
) -> List[Dict[str, Any]]:
    os.makedirs(out_dir, exist_ok=True)
    findings_all: List[Dict[str, Any]] = []

    q: asyncio.Queue[str] = asyncio.Queue()
    for u in urls:
        q.put_nowait(u)

    completed = 0
    total = q.qsize()
    lock = asyncio.Lock()

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        async def worker(worker_id: int):
            nonlocal completed
            page = await context.new_page()
            try:
                while True:
                    try:
                        url = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    if rps > 0:
                        await asyncio.sleep(1.0 / rps)

                    result = await process_url(page, url, marks, save_screenshots, out_dir)
                    findings_all.extend(result)

                    # ---- Visible progress ----
                    async with lock:
                        completed += 1
                        if completed % 5 == 0 or completed == total:
                            print(f"[audit] {completed}/{total} URLs processed", flush=True)

                    q.task_done()
            finally:
                await page.close()

        num_workers = max(1, min(concurrency, total))
        workers = [asyncio.create_task(worker(i)) for i in range(num_workers)]
        await asyncio.gather(*workers)
        await context.close()
        await browser.close()

    return findings_all


# -----------------------------
# Outputs
# -----------------------------
def render_html_report(rows: List[Dict[str, Any]]) -> str:
    head = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Vitamix Trademark Audit — Report</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px}
.badge{display:inline-block;padding:.1rem .4rem;border-radius:.4rem;font-size:.8rem}
.tbl{width:100%;border-collapse:collapse;margin-top:16px}
.tbl th,.tbl td{border-bottom:1px solid #eee;padding:8px;text-align:left;vertical-align:top}
.url a{word-break:break-all}
.snip{white-space:pre-wrap}
.issue-missing{background:#ffe5e5}
.issue-wrong{background:#fff3cd}
.issue-notfound{background:#eef7ff}
</style></head><body>
<h1>Vitamix Trademark Audit — Report</h1>
<p>Only text marks; logos/design marks are ignored. Screenshots saved only for flagged pages.</p>
<table class="tbl">
<thead><tr>
  <th>URL</th><th>Term</th><th>Issue</th><th>Expected</th><th>Found</th><th>Path</th><th>Snippet</th>
</tr></thead><tbody>
"""
    body_parts = []
    for r in rows:
        cls = (
            "issue-missing" if r.get("issue") == "missing symbol"
            else "issue-wrong" if r.get("issue") == "wrong symbol"
            else "issue-notfound" if r.get("issue") == "not found in prominent text"
            else ""
        )
        body_parts.append(
            "<tr class=\"%s\"><td class=\"url\"><a href=\"%s\" target=\"_blank\">%s</a></td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td><code>%s</code></td><td class=\"snip\">%s</td></tr>"
            % (
                cls,
                html_escape(r.get("url", "")),
                html_escape(r.get("url", "")),
                html_escape(r.get("term", "")),
                html_escape(r.get("issue", "")),
                html_escape(r.get("expected", "")),
                html_escape(r.get("found", "")),
                html_escape((r.get("path") or "")[:140]),
                html_escape(r.get("snippet", "")),
            )
        )

    tail = """
</tbody></table>
</body></html>
"""
    return head + "\n".join(body_parts) + tail


def write_outputs(findings: List[Dict[str, Any]], out_dir: str):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rows = findings  # already dicts

    # CSV
    pd.DataFrame(rows).to_csv(out / "findings.csv", index=False)

    # JSONL
    with open(out / "findings.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # HTML
    html = render_html_report(rows)
    (out / "report.html").write_text(html, encoding="utf-8")


# -----------------------------
# CLI
# -----------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Vitamix trademark audit tool (prototype)")
    parser.add_argument("--urls-file", required=True, help="CSV file containing URLs to audit (column name: url)")
    parser.add_argument("--marks-file", required=True, help="JSON file containing trademark definitions")
    parser.add_argument("--out", required=True, help="Output directory for reports and screenshots")
    parser.add_argument("--concurrency", type=int, default=4, help="Number of concurrent pages to use")
    parser.add_argument("--rps", type=float, default=2.0, help="Maximum requests per second (polite rate)")
    parser.add_argument("--save-flagged-screenshots", action="store_true", help="Save screenshots for pages with findings")
    args = parser.parse_args()

    urls = read_urls(args.urls_file)
    marks = read_marks(args.marks_file)

    findings = asyncio.run(
        run_audit(
            urls=urls,
            marks=marks,
            out_dir=args.out,
            concurrency=args.concurrency,
            rps=args.rps,
            save_screenshots=args.save_flagged_screenshots,
        )
    )
    write_outputs(findings, args.out)


if __name__ == "__main__":
    main()
