"""
Momox ISBN Daily Scanner Agent
================================
Uses the official Momox API v4 endpoint (api.momox.de)
which does not return 403 errors like the website scraping approach.

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
    # Add your ISBNs here, one per line, in quotes, comma at the end
]

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "from_email": "your.email@gmail.com",
    "app_password": os.environ.get("EMAIL_PASSWORD", ""),
    "to_email": "recipient@example.com",
}

MOMOX_API_TOKEN = "2231443b8fb511c7b6a0eb25a62577320bac69b6"
MOMOX_MARKETPLACE = "momox_de"

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
# MOMOX API
# ──────────────────────────────────────────────

API_HEADERS = {
    "X-API-TOKEN": MOMOX_API_TOKEN,
    "X-MARKETPLACE-ID": MOMOX_MARKETPLACE,
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}


def check_isbn_on_momox(isbn):
    url = "https://api.momox.de/api/v4/offer/?ean=" + isbn
    try:
        response = requests.get(url, headers=API_HEADERS, timeout=10)
        log.info("ISBN " + isbn + " -> HTTP " + str(response.status_code))

        if response.status_code == 200:
            data = response.json()
            status = data.get("status", "unknown")
            price = data.get("price")
            title = data.get("title") or data.get("name") or "Unknown title"
            available = status == "offer" and price is not None
            return {
                "isbn": isbn,
                "available": available,
                "status": status,
                "price": price,
                "title": title,
                "url": "https://www.momox.de/suche/?q=" + isbn,
                "error": None,
            }
        elif response.status_code == 404:
            return {
                "isbn": isbn,
                "available": False,
                "status": "not_found",
                "price": None,
                "title": None,
                "url": "https://www.momox.de/suche/?q=" + isbn,
                "error": "Not found (404)",
            }
        else:
            return {
                "isbn": isbn,
                "available": False,
                "status": "error",
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
            "status": "error",
            "price": None,
            "title": None,
            "url": None,
            "error": str(e),
        }


def check_multiple_isbns(isbns):
    if len(isbns) == 0:
        return []

    ean_string = ",".join(isbns)
    url = "https://api.momox.de/api/v4/quicksell/?eans=" + ean_string
    try:
        response = requests.get(url, headers=API_HEADERS, timeout=15)
        if response.status_code == 200:
            data = response.json()
            results = []
            for isbn in isbns:
                item = data.get(isbn, {})
                status = item.get("status", "unknown")
                price = item.get("price")
                title = item.get("title") or item.get("name") or "Unknown title"
                available = status == "offer" and price is not None
                results.append({
                    "isbn": isbn,
                    "available": available,
                    "status": status,
                    "price": price,
                    "title": title,
                    "url": "https://www.momox.de/suche/?q=" + isbn,
                    "error": None,
                })
            return results
    except Exception as e:
        log.warning("Bulk endpoint failed, falling back: " + str(e))

    results = []
    for i, isbn in enumerate(isbns, 1):
        log.info("Scanning " + str(i) + "/" + str(len(isbns)) + ": " + isbn)
        result = check_isbn_on_momox(isbn)
        results.append(result)
        if i < len(isbns):
            time.sleep(DELAY_BETWEEN_REQUESTS)
    return results


def scan_all_isbns(isbns):
    all_results = []
    batch_size = 9
    for i in range(0, len(isbns), batch_size):
        batch = isbns[i:i + batch_size]
        log.info("Scanning batch of " + str(len(batch)) + " ISBNs")
        results = check_multiple_isbns(batch)
        all_results.extend(results)
        if i + batch_size < len(isbns):
            time.sleep(DELAY_BETWEEN_REQUESTS)
    return all_results

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
        return "(first scan)"
    was_available = yesterday.get("available", False)
    if not was_available and currently_available:
        return "*** NOW AVAILABLE ***"
    if was_available and not currently_available:
        return "(no longer available)"
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

    lines = []
    lines.append("Momox ISBN Daily Report - " + today)
    lines.append("=" * 50)
    lines.append("Total scanned: " + str(len(results)))
    lines.append("Momox will buy: " + str(len(available)))
    lines.append("Not buying: " + str(len(not_available)))
    lines.append("Errors: " + str(len(errors)))
    lines.append("")

    if available:
        lines.append("AVAILABLE FOR SALE")
        lines.append("-" * 30)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price_str = "EUR " + str(r["price"]) if r["price"] else "price unknown"
            lines.append("  " + r["isbn"] + " | " + str(r.get("title", "?")) + " | " + price_str + " " + change)
        lines.append("")

    if not_available:
        lines.append("NOT AVAILABLE")
        lines.append("-" * 30)
        for r in not_available:
            change = get_status_change(r["isbn"], False, history)
            lines.append("  " + r["isbn"] + " | " + str(r.get("title", "?")) + " " + change)
        lines.append("")

    if errors:
        lines.append("ERRORS")
        lines.append("-" * 30)
        for r in errors:
            lines.append("  " + r["isbn"] + " - " + str(r["error"]))
        lines.append("")

    plain_text = "\n".join(lines)

    table_style = "border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%'"

    if available:
        rows = make_row(["ISBN", "Title", "Price", "Change", "Link"], header=True)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price = "EUR " + str(r["price"]) if r["price"] else "?"
            link = '<a href="' + r["url"] + '">View</a>' if r.get("url") else ""
            rows = rows + make_row([r["isbn"], r.get("title") or "?", price, change, link])
        available_html = "<table " + table_style + "><thead style='background:#e8f5e9'>" + rows + "</thead></table>"
    else:
        available_html = "<p>Momox is not buying any of your ISBNs today.</p>"

    if not_available:
        rows = make_row(["ISBN", "Title", "Status", "Change"], header=True)
        for r in not_available:
            change = get_status_change(r["isbn"], False, history)
            rows = rows + make_row([r["isbn"], r.get("title") or "?", r.get("status", "?"), change])
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
        "<h3 style='color:green'>Momox will BUY these (" + str(len(available)) + ")</h3>"
        + available_html
        + "<h3 style='color:#c0392b'>Not buying these today (" + str(len(not_available)) + ")</h3>"
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
    log.info("Scanning " + str(len(ISBNS)) + " ISBNs via Momox API v4...")

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
