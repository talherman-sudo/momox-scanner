"""
Momox ISBN Daily Scanner Agent
================================
Scans a list of ISBNs on Momox daily and sends a report by email.
Setup: pip install requests
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
    "9780141036144",
    "9780062316097",
    # Add your ISBNs here
]

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "from_email": "talherman@gmail.com",
    "app_password": os.environ.get("EMAIL_PASSWORD", ""),
    "to_email": "talherman@gmail.com",
}

DATA_FILE = "isbn_history.json"
DELAY_BETWEEN_REQUESTS = 1.5

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


def check_isbn_on_momox(isbn):
    url = "https://www.momox.de/api/v2/offer?ean=" + isbn
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
                "url": "https://www.momox.de/suche/?q=" + isbn,
                "error": None,
            }
        elif response.status_code == 404:
            return {
                "isbn": isbn,
                "available": False,
                "price": None,
                "title": None,
                "url": "https://www.momox.de/suche/?q=" + isbn,
                "error": "Not found (404)",
            }
        else:
            return {
                "isbn": isbn,
                "available": False,
                "price": None,
                "title": None,
                "url": None,
                "error": "HTTP " + str(response.status_code),
            }
    except requests.exceptions.RequestException as e:
        log.error("Request failed for ISBN " + isbn + ": " + str(e))
        return {
            "isbn": isbn,
            "available": False,
            "price": None,
            "title": None,
            "url": None,
            "error": str(e),
        }


def scan_all_isbns(isbns):
    results = []
    for i, isbn in enumerate(isbns, 1):
        log.info("Scanning " + str(i) + "/" + str(len(isbns)) + ": ISBN " + isbn)
        result = check_isbn_on_momox(isbn)
        results.append(result)
        if i < len(isbns):
            time.sleep(DELAY_BETWEEN_REQUESTS)
    return results

# ──────────────────────────────────────────────
# HISTORY
# ──────────────────────────────────────────────

def load_history():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}


def save_history(history):
    with open(DATA_FILE, "w") as f:
        json.dump(history, f, indent=2)


def get_status_change(isbn, currently_available, history):
    yesterday = history.get(isbn)
    if yesterday is None:
        return "(new)"
    was_available = yesterday.get("available", False)
    if not was_available and currently_available:
        return "(NOW AVAILABLE - was not yesterday)"
    if was_available and not currently_available:
        return "(NO LONGER AVAILABLE - was available yesterday)"
    return ""

# ──────────────────────────────────────────────
# REPORT GENERATOR
# ──────────────────────────────────────────────

def make_row(cells, header=False):
    tag = "th" if header else "td"
    inner = ""
    for c in cells:
        inner = inner + "<" + tag + ">" + str(c) + "</" + tag + ">"
    return "<tr>" + inner + "</tr>"


def generate_report(results, history):
    today = date.today().strftime("%A, %d %B %Y")
    available = [r for r in results if r["available"]]
    not_available = [r for r in results if not r["available"] and not r["error"]]
    errors = [r for r in results if r["error"]]

    # Plain text
    lines = []
    lines.append("Momox ISBN Daily Report - " + today)
    lines.append("=" * 50)
    lines.append("Total scanned: " + str(len(results)))
    lines.append("Available for sale: " + str(len(available)))
    lines.append("Not available: " + str(len(not_available)))
    lines.append("Errors: " + str(len(errors)))
    lines.append("")

    if available:
        lines.append("AVAILABLE FOR SALE")
        lines.append("-" * 30)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price_str = "EUR " + str(r["price"]) if r["price"] else "price unknown"
            lines.append("  " + r["isbn"] + " | " + str(r.get("title", "?")) + " | " + price_str + " " + change)
            if r.get("url"):
                lines.append("  -> " + r["url"])
        lines.append("")

    if not_available:
        lines.append("NOT AVAILABLE")
        lines.append("-" * 30)
        for r in not_available:
            change = get_status_change(r["isbn"], False, history)
            title = r.get("title") or "unknown"
            lines.append("  " + r["isbn"] + " | " + title + " " + change)
        lines.append("")

    if errors:
        lines.append("ERRORS")
        lines.append("-" * 30)
        for r in errors:
            lines.append("  " + r["isbn"] + " - " + str(r["error"]))
        lines.append("")

    plain_text = "\n".join(lines)

    # HTML
    table_style = "border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%'"

    if available:
        rows = make_row(["ISBN", "Title", "Price", "Change", "Link"], header=True)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price = "EUR " + str(r["price"]) if r["price"] else "?"
            link = '<a href="' + r["url"] + '">View on Momox</a>' if r.get("url") else ""
            rows = rows + make_row([r["isbn"], r.get("title") or "?", price, change, link])
        available_html = "<table " + table_style + "><thead style='background:#e8f5e9'>" + rows + "</thead></table>"
    else:
        available_html = "<p>None today.</p>"

    if not_available:
        rows = make_row(["ISBN", "Title", "Change"], header=True)
        for r in not_available:
            change = get_status_change(r["isbn"], False, history)
            rows = rows + make_row([r["isbn"], r.get("title") or "?", change])
        na_html = "<table " + table_style + "><thead style='background:#fdecea'>" + rows + "</thead></table>"
    else:
        na_html = "<p>None today.</p>"

    if errors:
        rows = make_row(["ISBN", "Error"], header=True)
        for r in errors:
            rows = rows + make_row([r["isbn"], r["error"]])
        err_html = (
            "<h3 style='color:orange'>Errors (" + str(len(errors)) + ")</h3>"
            "<table " + table_style + "><thead style='background:#fff3e0'>" + rows + "</thead></table>"
        )
    else:
        err_html = ""

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = (
        "<html><body style='font-family:Arial,sans-serif;max-width:700px;margin:auto'>"
        "<h2 style='color:#333'>Momox ISBN Report</h2>"
        "<p style='color:#666'>" + today + " &mdash; " + str(len(results)) + " ISBNs scanned</p>"
        "<h3 style='color:green'>Available (" + str(len(available)) + ")</h3>"
        + available_html
        + "<h3 style='color:#c0392b'>Not Available (" + str(len(not_available)) + ")</h3>"
        + na_html
        + err_html
        + "<p style='color:#aaa;font-size:12px;margin-top:30px'>Generated by Momox ISBN Agent &mdash; "
        + timestamp + "</p>"
        "</body></html>"
    )

    return plain_text, html

# ──────────────────────────────────────────────
# EMAIL SENDER
# ──────────────────────────────────────────────

def send_email(plain_text, html, config):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Momox ISBN Report - " + str(date.today())
    msg["From"] = config["from_email"]
    msg["To"] = config["to_email"]
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL(config["smtp_server"], config["smtp_port"]) as server:
        server.login(config["from_email"], config["app_password"])
        server.sendmail(config["from_email"], config["to_email"], msg.as_string())

    log.info("Report emailed to " + config["to_email"])

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    log.info("=== Momox ISBN Agent starting ===")
    log.info("Scanning " + str(len(ISBNS)) + " ISBNs...")

    history = load_history()
    results = scan_all_isbns(ISBNS)
    plain_text, html = generate_report(results, history)

    print(plain_text)

    try:
        send_email(plain_text, html, EMAIL_CONFIG)
    except Exception as e:
        log.error("Failed to send email: " + str(e))
        raise

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
