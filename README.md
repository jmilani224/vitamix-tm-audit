# Vitamix Trademark Symbol Audit Tool

This repository contains a simple prototype tool for auditing Vitamix
web pages for correct trademark symbol usage. It is intended to be
run as a GitHub Actions workflow or via the command line.

## Contents

* `audit.py` – The main Python script that crawls a list of URLs,
  evaluates the first prominent mention of each trademark term and
  reports missing or incorrect symbols.
* `requirements.txt` – Python dependencies (currently only
  `playwright`).
* `.github/workflows/run-audit.yml` – GitHub Actions workflow that
  installs dependencies, runs the audit over a URL list and uploads
  the results as artifacts.
* `top_urls_clean_us_en.csv` – Example input file with a list of
  Vitamix URLs (one per line in a `url` column) for `/us/en` paths.
* `trademarks_us_2025_text_only.json` – Trademark definitions used
  during auditing. Each entry includes the term, required symbol
  (® or ™), and optional variants.

## Running the audit via GitHub Actions

1. Commit all files to a new private repository on GitHub.
2. Navigate to the **Actions** tab and enable workflows if prompted.
3. Under **Actions**, locate **Vitamix Trademark Audit** and click
   **Run workflow**. You can leave the inputs blank since the
   workflow uses the committed CSV and JSON by default.
4. After the job completes, download the `vitamix-audit-results` artifact
   from the run summary. It contains:
   - `findings.csv` – CSV summary of each finding.
   - `findings.jsonl` – JSON Lines format of the findings.
   - `report.html` – A human‑friendly report.
   - Screenshots (PNG) for pages with issues, if enabled.

## Running locally

If you prefer to run the audit locally rather than through GitHub
Actions, install dependencies and run the script:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python audit.py \
  --urls-file top_urls_clean_us_en.csv \
  --marks-file trademarks_us_2025_text_only.json \
  --out runs/$(date +%Y%m%d_%H%M%S) \
  --concurrency 4 \
  --rps 2 \
  --save-flagged-screenshots
```

See the header of `audit.py` for additional documentation on
command‑line options.