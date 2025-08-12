#!/usr/bin/env python3
"""
Audit Vitamix pages for proper trademark symbol usage.

This script reads a list of URLs from a CSV file, fetches each page
with Playwright and inspects the "prominent" text on the page for
configured trademark terms. It reports when required symbols (®/™)
are missing or incorrect on the first prominent mention.

The goal of this prototype is to be simple enough to run in
GitHub Actions while still catching the majority of missing
or incorrect trademark symbols. It does **not** attempt to
replicate every nuance of the full specification. You should
review the output and adjust the heuristics as needed.

Usage:
    python audit.py \
      --urls-file top_urls_clean_us_en.csv \
      --marks-file trademarks_us_2025_text_only.json \
      --out runs/out \
      --concurrency 4 --rps 2 \
      --save-flagged-screenshots

The CSV file must contain a column named "url". The marks JSON
should follow this schema:

```
{
  "marks": [
    {
      "term": "Vitamix",
      "symbol": "®",
      "variants": [],
      "case_insensitive": true,
      "policy": "first_prominent_only",
      "locales": ["en-US"]
    },
    ...
  ]
}
```

The script writes three output files into the ``--out`` directory:
    - findings.csv: machine‑readable summary of every issue found.
    - findings.jsonl: JSON Lines version of the same data.
    - report.html: simple human‑friendly report.

If ``--save-flagged-screenshots`` is provided, the script saves
screenshot PNGs for pages that have at least one finding. The
screenshots help with visual verification but are optional.
"""

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime

from playwright.async_api import async_playwright, Browser, Page


def read_urls(csv_path: str) -> list[str]:
    """Read a list of URLs from a CSV file with column 'url'."""
    urls = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if 'url' not in reader.fieldnames:
            raise ValueError(f"CSV file {csv_path} must contain a 'url' column")
        for row in reader:
            url = row['url'].strip()
            if url:
                urls.append(url)
    return urls


def read_marks(json_path: str) -> list[dict]:
    """Load mark definitions from the given JSON file."""
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)
    marks = data.get('marks', [])
    # Normalize terms: strip whitespace
    for mark in marks:
        mark['term'] = str(mark['term']).strip()
        mark['symbol'] = str(mark.get('symbol', '')).strip()
        # Force case insensitivity flag to boolean
        mark['case_insensitive'] = bool(mark.get('case_insensitive', True))
    return marks


async def evaluate_prominent_elements(page: Page) -> list[dict]:
    """Return a list of candidate nodes for prominent text.

    Each element contains its text content, DOM path and inner HTML.
    The order of candidates is significant – earlier entries are
    considered more prominent.
    """
    script = """
    () => {
        function isVisible(el) {
            const rect = el.getBoundingClientRect();
            return (rect.width > 0 && rect.height > 0);
        }
        const root = document.querySelector('main') || document.body;
        const candidates = [];
        function addAll(selector) {
            root.querySelectorAll(selector).forEach(el => {
                if (isVisible(el)) candidates.push(el);
            });
        }
        // H1/H2/H3 tags
        ['h1','h2','h3'].forEach(sel => addAll(sel));
        // Hero and title classes
        ['.hero','.product-title','.page-intro','.pdp-title','.tile-title'].forEach(sel => addAll(sel));
        // First visible <p> or <li>
        const textNodes = Array.from(root.querySelectorAll('p, li'));
        for (const el of textNodes) {
            if (isVisible(el)) {
                candidates.push(el);
                break;
            }
        }
        // Build result objects
        return candidates.map(el => {
            let path = '';
            let node = el;
            const parts = [];
            while (node && node.nodeType === Node.ELEMENT_NODE) {
                let part = node.tagName.toLowerCase();
                if (node.id) part += '#' + node.id;
                if (node.className) part += '.' + Array.from(node.classList).join('.');
                parts.unshift(part);
                node = node.parentElement;
            }
            return {
                text: el.textContent || '',
                path: parts.join(' > '),
                html: el.innerHTML || ''
            };
        });
    }
    """
    return await page.evaluate(script)


def find_first_term(text: str, term: str, case_insensitive: bool) -> int:
    """Return the index of the first occurrence of term in text (or -1)."""
    if case_insensitive:
        idx = text.lower().find(term.lower())
    else:
        idx = text.find(term)
    return idx


async def process_url(page: Page, url: str, marks: list[dict], save_screenshot: bool, out_dir: str) -> list[dict]:
    """Process a single URL and return a list of findings."""
    findings: list[dict] = []
    try:
        await page.goto(url, wait_until='networkidle', timeout=60000)
    except Exception as exc:
        findings.append({
            'url': url,
            'term': '',
            'issue': 'navigation error',
            'expected': '',
            'found': '',
            'path': '',
            'snippet': '',
            'details': str(exc)
        })
        return findings
    candidates = []
    try:
        candidates = await evaluate_prominent_elements(page)
    except Exception as exc:
        findings.append({
            'url': url,
            'term': '',
            'issue': 'evaluation error',
            'expected': '',
            'found': '',
            'path': '',
            'snippet': '',
            'details': str(exc)
        })
        return findings
    page_has_issue = False
    for mark in marks:
        term = mark['term']
        symbol = mark['symbol']
        ci = mark.get('case_insensitive', True)
        issue_recorded = False
        for candidate in candidates:
            text = candidate['text']
            idx = find_first_term(text, term, ci)
            if idx != -1:
                # Found term; determine whether correct symbol follows immediately.
                end = idx + len(term)
                actual_symbol = ''
                if end < len(text):
                    # skip whitespace and punctuation between term and symbol
                    j = end
                    while j < len(text) and text[j].isspace():
                        j += 1
                    if j < len(text):
                        # take one char after spacing
                        actual_symbol = text[j]
                if actual_symbol == symbol:
                    # Correct symbol – no issue, record nothing
                    pass
                elif actual_symbol in ['®', '™'] and actual_symbol != symbol:
                    findings.append({
                        'url': url,
                        'term': term,
                        'issue': 'wrong symbol',
                        'expected': symbol,
                        'found': actual_symbol,
                        'path': candidate['path'],
                        'snippet': text.strip()[:200],
                        'details': ''
                    })
                    issue_recorded = True
                    page_has_issue = True
                else:
                    findings.append({
                        'url': url,
                        'term': term,
                        'issue': 'missing symbol',
                        'expected': symbol,
                        'found': actual_symbol or '',
                        'path': candidate['path'],
                        'snippet': text.strip()[:200],
                        'details': ''
                    })
                    issue_recorded = True
                    page_has_issue = True
                break  # Only inspect first occurrence
        if not issue_recorded:
            # Term not found in any prominent text
            findings.append({
                'url': url,
                'term': term,
                'issue': 'not found in prominent text',
                'expected': symbol,
                'found': '',
                'path': '',
                'snippet': '',
                'details': ''
            })
    # Save screenshot if needed
    if save_screenshot and page_has_issue:
        ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        basename = f"{url.replace('://', '__').replace('/', '_')}_{ts}.png"
        filepath = os.path.join(out_dir, basename)
        try:
            await page.screenshot(path=filepath, full_page=True)
        except Exception:
            pass
    return findings


async def run_audit(urls: list[str], marks: list[dict], out_dir: str, concurrency: int, rps: float, save_screenshots: bool) -> list[dict]:
    """Run the audit across all URLs with concurrency and rate limiting."""
    os.makedirs(out_dir, exist_ok=True)
    findings_all: list[dict] = []
    sem = asyncio.Semaphore(concurrency)
    # Simple rate limiter (rudimentary): sleeps after each batch of concurrency
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        page_pool = [await browser.new_page() for _ in range(concurrency)]
        async def worker(idx: int):
            page = page_pool[idx]
            # allow persistent context for concurrency
            pass
        async def handle_url(index: int, url: str, page: Page):
            async with sem:
                result = await process_url(page, url, marks, save_screenshots, out_dir)
                findings_all.extend(result)
        tasks = []
        # assign pages round‑robin to concurrency pages
        for i, url in enumerate(urls):
            page = page_pool[i % concurrency]
            tasks.append(asyncio.create_task(handle_url(i, url, page)))
            # Rate limiting: sleep after scheduling each URL based on rps
            if rps > 0:
                await asyncio.sleep(1.0 / rps)
        await asyncio.gather(*tasks)
        await browser.close()
    return findings_all


def write_outputs(findings: list[dict], out_dir: str):
    """Write findings to CSV, JSONL and a simple HTML report."""
    csv_path = os.path.join(out_dir, 'findings.csv')
    json_path = os.path.join(out_dir, 'findings.jsonl')
    html_path = os.path.join(out_dir, 'report.html')
    fieldnames = ['url', 'term', 'issue', 'expected', 'found', 'path', 'snippet', 'details']
    # Write CSV
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in findings:
            writer.writerow({k: item.get(k, '') for k in fieldnames})
    # Write JSONL
    with open(json_path, 'w', encoding='utf-8') as f:
        for item in findings:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
    # Write simple HTML report
    rows = []
    for item in findings:
        rows.append(
            f"<tr><td>{item['url']}</td><td>{item['term']}</td>"
            f"<td>{item['issue']}</td><td>{item['expected']}</td>"
            f"<td>{item['found']}</td><td>{item['path']}</td>"
            f"<td>{item['snippet'].replace('<', '&lt;').replace('>', '&gt;')}</td>"
            f"</tr>"
        )
    html = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Vitamix Trademark Audit Report</title>
<style>
body { font-family: sans-serif; margin: 1em; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ccc; padding: 4px; }
th { background: #f0f0f0; }
tr:nth-child(even) { background: #fafafa; }
</style></head><body>
<h1>Vitamix Trademark Audit Report</h1>
<p>Generated: {timestamp}</p>
<table>
  <thead>
    <tr><th>URL</th><th>Term</th><th>Issue</th><th>Expected</th><th>Found</th><th>Path</th><th>Snippet</th></tr>
  </thead>
  <tbody>
  {rows}
  </tbody>
</table>
</body></html>
""".format(timestamp=datetime.utcnow().isoformat() + 'Z', rows='\n  '.join(rows))
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)


def main() -> None:
    parser = argparse.ArgumentParser(description='Vitamix trademark audit tool (prototype)')
    parser.add_argument('--urls-file', required=True, help='CSV file containing URLs to audit (column name: url)')
    parser.add_argument('--marks-file', required=True, help='JSON file containing trademark definitions')
    parser.add_argument('--out', required=True, help='Output directory for reports and screenshots')
    parser.add_argument('--concurrency', type=int, default=4, help='Number of concurrent pages to use')
    parser.add_argument('--rps', type=float, default=2.0, help='Maximum requests per second (polite rate)')
    parser.add_argument('--save-flagged-screenshots', action='store_true', help='Save screenshots for pages with findings')
    args = parser.parse_args()
    urls = read_urls(args.urls_file)
    marks = read_marks(args.marks_file)
    findings = asyncio.run(run_audit(urls, marks, args.out, args.concurrency, args.rps, args.save_flagged_screenshots))
    write_outputs(findings, args.out)


if __name__ == '__main__':
    main()