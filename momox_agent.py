"""
Momox ISBN Daily Scanner Agent
================================
Scans a list of ISBNs on Momox daily and sends a report by email.

Setup:
  pip install requests

Configuration:
  Edit the CONFIG section below, then run:
    python momox_agent.py

To schedule daily (see README at the bottom of this file).
"""

import os
import json
import time
import smtplib
import logging
import requests
from datetime import date, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ──────────────────────────────────────────────
# CONFIG — edit this section
# ──────────────────────────────────────────────

ISBNS = [
    "9780141036144",   # 1984 – George Orwell
    "9780062316097",   # The Alchemist – Paulo Coelho
    # Add as many ISBNs as you want here
]

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "from_email": "your.email@gmail.com",      # ← your Gmail address
    "app_password": "xxxx xxxx xxxx xxxx",     # ← Gmail App Password (not your main password)
    "to_email": "recipient@example.com",       # ← where to send the report
}

DATA_FILE = "isbn_history.json"    # stores daily results for tracking trends
DELAY_BETWEEN_REQUESTS = 1.5       # seconds between Momox requests (be polite!)

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("momox_agent.log"),
    ]
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# MOMOX SCRAPER
# ──────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://www.momox.de/",
}

def check_isbn_on_momox(isbn: str) -> dict:
    """
    Query Momox for a single ISBN.
    Returns a dict with isbn, available, price, title, and raw response data.
    
    NOTE: Momox does not have a public documented API.
    The endpoint below is based on reverse-engineering their site.
    If it stops working, open momox.de in Chrome DevTools > Network tab,
    scan an EAN, and look for the JSON API call to update this URL.
    """
    url = f"https://www.momox.de/api/v2/offer?ean={isbn}"
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            price = data.get("price") or data.get("purchasePrice")
            title = data.get("title") or data.get("name") or "Unknown title"
            available = price is not None and float(str(price).replace(",", ".")) > 0

            return {
                "isbn": isbn,
                "available": available,
                "price": price,
                "title": title,
                "url": f"https://www.momox.de/suche/?q={isbn}",
                "error": None,
                "raw": data,
            }
        
        elif response.status_code == 404:
            return {
                "isbn": isbn,
                "available": False,
                "price": None,
                "title": None,
                "url": f"https://www.momox.de/suche/?q={isbn}",
                "error": "Not found (404)",
                "raw": {},
            }
        
        else:
            log.warning(f"ISBN {isbn} — HTTP {response.status_code}")
            return {
                "isbn": isbn,
                "available": False,
                "price": None,
                "title": None,
                "url": None,
                "error": f"HTTP {response.status_code}",
                "raw": {},
            }

    except requests.exceptions.RequestException as e:
        log.error(f"Request failed for ISBN {isbn}: {e}")
        return {
            "isbn": isbn,
            "available": False,
            "price": None,
            "title": None,
            "url": None,
            "error": str(e),
            "raw": {},
        }


def scan_all_isbns(isbns: list) -> list:
    """Scan all ISBNs with a polite delay between requests."""
    results = []
    for i, isbn in enumerate(isbns, 1):
        log.info(f"Scanning {i}/{len(isbns)}: ISBN {isbn}")
        result = check_isbn_on_momox(isbn)
        results.append(result)
        if i < len(isbns):
            time.sleep(DELAY_BETWEEN_REQUESTS)
    return results

# ──────────────────────────────────────────────
# HISTORY (tracks changes day-to-day)
# ──────────────────────────────────────────────

def load_history() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}


def save_history(history: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(history, f, indent=2)


def get_status_change(isbn: str, currently_available: bool, history: dict) -> str:
    """Returns a change label if availability changed since yesterday."""
    yesterday_entry = history.get(isbn)
    if yesterday_entry is None:
        return "🆕 NEW"
    was_available = yesterday_entry.get("available", False)
    if not was_available and currently_available:
        return "🟢 NOW AVAILABLE"
    if was_available and not currently_available:
        return "🔴 NO LONGER AVAILABLE"
    return ""

# ──────────────────────────────────────────────
# REPORT GENERATOR
# ──────────────────────────────────────────────

def generate_report(results: list, history: dict) -> tuple:
    """Returns (plain_text_report, html_report)."""
    today = date.today().strftime("%A, %d %B %Y")
    available = [r for r in results if r["available"]]
    not_available = [r for r in results if not r["available"] and not r["error"]]
    errors = [r for r in results if r["error"]]

    # ── Plain text ──────────────────────────────
    lines = [
        f"Momox ISBN Daily Report — {today}",
        "=" * 50,
        f"Total scanned: {len(results)}",
        f"Available for sale: {len(available)}",
        f"Not available: {len(not_available)}",
        f"Errors: {len(errors)}",
        "",
    ]

    if available:
        lines.append("✅ AVAILABLE FOR SALE")
        lines.append("-" * 30)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price_str = f"€{r['price']}" if r["price"] else "price unknown"
            lines.append(f"  {r['isbn']} | {r.get('title','?')} | {price_str} {change}")
            lines.append(f"  → {r['url']}")
        lines.append("")

    if not_available:
        lines.append("❌ NOT AVAILABLE")
        lines.append("-" * 30)
        for r in not_available:
            change = get_status_change(r["isbn"], False, history)
            title = r.get("title") or "unknown"
            lines.append(f"  {r['isbn']} | {title} {change}")
        lines.append("")

    if errors:
        lines.append("⚠️  ERRORS")
        lines.append("-" * 30)
        for r in errors:
            lines.append(f"  {r['isbn']} — {r['error']}")
        lines.append("")

    plain_text = "\n".join(lines)

    # ── HTML ────────────────────────────────────
    def row(cells, header=False):
        tag = "th" if header else "td"
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"

    available_rows = "".join(
        row([
            r["isbn"],
            r.get("title") or "?",
            f"€{r['price']}" if r["price"] else "?",
            get_status_change(r["isbn"], True, history),
            f'<a href="{r["url"]}">View</a>' if r.get("url") else "",
        ])
        for r in available
    )

    not_available_rows = "".join(
        row([
            r["isbn"],
            r.get("title") or "?",
            get_status_change(r["isbn"], False, history),
        ])
        for r in not_available
    )

    error_rows = "".join(
        row([r["isbn"], r["error"]])
        for r in errors
    )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto">
    <h2 style="color:#333">📚 Momox ISBN Report</h2>
    <p style="color:#666">{today} &mdash; {len(results)} ISBNs scanned</p>

    <h3 style="color:green">✅ Available ({len(available)})</h3>
    {"<p>None today.</p>" if not available else f"""
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
      <thead style="background:#e8f5e9">{row(["ISBN","Title","Price","Change","Link"],True)}</thead>
      <tbody>{available_rows}</tbody>
    </table>"""}

    <h3 style="color:#c0392b">❌ Not Available ({len(not_available)})</h3>
    {"<p>None today.</p>" if not not_available else f"""
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
      <thead style="background:#fdecea">{row(["ISBN","Title","Change"],True)}</thead>
      <tbody>{not_available_rows}</tbody>
    </table>"""}

    {"" if not errors else f"""
    <h3 style="color:orange">⚠️ Errors ({len(errors)})</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
      <thead style="background:#fff3e0">{row(["ISBN","Error"],True)}</thead>
      <tbody>{error_rows}</tbody>
    </table>"""}

    <p style="color:#aaa;font-size:12px;margin-top:30px">
      Generated by Momox ISBN Agent &mdash; {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    </p>
    </body></html>
    """

    return plain_text, html

# ──────────────────────────────────────────────
# EMAIL SENDER
# ──────────────────────────────────────────────

def send_email(plain_text: str, html: str, config: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📚 Momox ISBN Report — {date.today()}"
    msg["From"] = config["from_email"]
    msg["To"] = config["to_email"]

    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL(config["smtp_server"], config["smtp_port"]) as server:
        server.login(config["from_email"], config["app_password"])
        server.sendmail(config["from_email"], config["to_email"], msg.as_string())

    log.info(f"Report emailed to {config['to_email']}")

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    log.info("=== Momox ISBN Agent starting ===")
    log.info(f"Scanning {len(ISBNS)} ISBNs...")

    # 1. Load yesterday's history
    history = load_history()

    # 2. Scan
    results = scan_all_isbns(ISBNS)

    # 3. Generate report
    plain_text, html = generate_report(results, history)
    print(plain_text)  # also print to console/logs

    # 4. Send email
    try:
        send_email(plain_text, html, EMAIL_CONFIG)
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        raise

    # 5. Save today's results to history
    today_str = date.today().isoformat()
    for r in results:
        history[r["isbn"]] = {
            "date": today_str,
            "available": r["available"],
            "price": r["price"],
            "title": r["title"],
        }
    save_history(history)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════
# README — HOW TO RUN THIS DAILY
# ══════════════════════════════════════════════════════════════════
#
# ── Prerequisites ──────────────────────────────────────────────
#   pip install requests
#
# ── Gmail App Password ─────────────────────────────────────────
#   1. Enable 2-Step Verification on your Google account
#   2. Go to: myaccount.google.com/apppasswords
#   3. Create a new App Password → copy the 16-char code
#   4. Paste it into EMAIL_CONFIG["app_password"] above
#
# ── Option A: Run locally with cron (Mac/Linux) ────────────────
#   Open terminal: crontab -e
#   Add this line (runs at 8:00 AM every day):
#     0 8 * * * /usr/bin/python3 /full/path/to/momox_agent.py >> /tmp/momox.log 2>&1
#
# ── Option B: Run locally on Windows (Task Scheduler) ──────────
#   1. Open Task Scheduler → Create Basic Task
#   2. Trigger: Daily at 08:00
#   3. Action: Start a program → python.exe
#   4. Arguments: C:\path\to\momox_agent.py
#
# ── Option C: GitHub Actions (free, cloud, recommended) ────────
#   1. Create a GitHub repo, push momox_agent.py into it
#   2. Store EMAIL_PASSWORD as a GitHub Secret (Settings → Secrets)
#   3. Create .github/workflows/daily_scan.yml:
#
#   name: Daily Momox Scan
#   on:
#     schedule:
#       - cron: '0 7 * * *'   # 7:00 UTC = 8:00 CET
#     workflow_dispatch:       # allows manual trigger too
#   jobs:
#     scan:
#       runs-on: ubuntu-latest
#       steps:
#         - uses: actions/checkout@v4
#         - name: Set up Python
#           uses: actions/setup-python@v5
#           with:
#             python-version: '3.11'
#         - name: Install deps
#           run: pip install requests
#         - name: Run agent
#           env:
#             EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
#           run: python momox_agent.py
#
#   NOTE: in GitHub Actions mode, read EMAIL_CONFIG["app_password"]
#   from os.environ["EMAIL_PASSWORD"] instead of hardcoding it.
#
# ── Option D: Railway / Render (cloud PaaS, very easy) ─────────
#   1. Push this file to a GitHub repo
#   2. Create account on railway.app or render.com
#   3. New project → deploy from GitHub repo
#   4. Set environment variables for email credentials
#   5. Set a cron schedule (Railway: Settings → Cron; Render: Cron Jobs)
#
# ══════════════════════════════════════════════════════════════════
