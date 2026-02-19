"""
Momox ISBN Daily Scanner Agent
================================
Uses ScraperAPI to fetch the Momox homepage search result for each ISBN.
Parses the price directly from the HTML page (same as what a user sees).

Setup: pip install requests beautifulsoup4
"""

import os
import re
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
    # Add your ISBNs here, one per line, in quotes, with a comma at the end
]

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "from_email": "talherman@gmail.com",
    "app_password": os.environ.get("EMAIL_PASSWORD", ""),
    "to_email": "talherman@gmail.com",
}

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

DATA_FILE = "isbn_history.json"
DELAY_BETWEEN_REQUESTS = 4.0   # seconds between requests

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
# MOMOX CHECKER
# ──────────────────────────────────────────────

def check_isbn_on_momox(isbn):
    """
    Fetches the Momox page for a given ISBN via ScraperAPI with JavaScript rendering.
    Parses the price from the rendered HTML.
    """
    # This is the URL that shows the buy price for a scanned item
    target_url = "https://www.momox.de/?search=" + isbn

    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "true",          # render JavaScript so price loads
        "country_code": "de",      # use German IP
        "wait_for_selector": "body",
    }

    try:
        log.info("Fetching ISBN " + isbn + " via ScraperAPI (JS render)...")
        response = requests.get(
            "https://api.scraperapi.com/",
            params=params,
            timeout=120,   # JS rendering needs more time
        )
        log.info("ISBN " + isbn + " -> HTTP " + str(response.status_code))

        if response.status_code != 200:
            return {
                "isbn": isbn,
                "available": False,
                "price": None,
                "title": None,
                "url": "https://www.momox.de/?search=" + isbn,
                "error": "HTTP " + str(response.status_code),
            }

        html = response.text
        log.info("Response length: " + str(len(html)) + " chars")
        log.info("Preview: " + html[:500])

        # Look for price pattern like "1,64" or "0,15" in the HTML
        # Momox shows "Du erhältst X,XX €"
        price = None
        title = None

        # Try to find the price — Momox shows it as e.g. "1,64" followed by €
        price_patterns = [
            r'Du\s+erh[äa]ltst[\s\S]{0,100}?(\d+[,\.]\d{2})\s*€',
            r'(\d+[,\.]\d{2})\s*€',
            r'"price":\s*"?(\d+[\.,]\d{2})"?',
            r'"purchasePrice":\s*"?(\d+[\.,]\d{2})"?',
            r'ankaufspreis[\s\S]{0,50}?(\d+[,\.]\d{2})',
        ]

        for pattern in price_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                price_str = match.group(1).replace(",", ".")
                try:
                    price_float = float(price_str)
                    if price_float > 0:
                        price = price_str
                        log.info("Found price: " + price)
                        break
                except ValueError:
                    continue

        # Try to find title
        title_patterns = [
            r'<title>([^<]+)</title>',
            r'"name":\s*"([^"]+)"',
            r'<h1[^>]*>([^<]+)</h1>',
        ]
        for pattern in title_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                title = match.group(1).strip()
                if len(title) > 5 and "momox" not in title.lower():
                    break

        # Check if Momox says it doesn't accept the item
        not_buying_signals = [
            "leider nicht ankaufen",
            "nicht angekauft",
            "nicht in unserem Sortiment",
            "Artikel wird leider nicht",
            "no_offer",
        ]
        for signal in not_buying_signals:
            if signal.lower() in html.lower():
                return {
                    "isbn": isbn,
                    "available": False,
                    "price": None,
                    "title": title,
                    "url": "https://www.momox.de/?search=" + isbn,
                    "error": None,
                }

        available = price is not None

        return {
            "isbn": isbn,
            "available": available,
            "price": price,
            "title": title,
            "url": "https://www.momox.de/?search=" + isbn,
            "error": None if available else "Price not found in page — check URL manually",
        }

    except requests.exceptions.Timeout:
        return {
            "isbn": isbn,
            "available": False,
            "price": None,
            "title": None,
            "url": None,
            "error": "Timed out after 120s",
        }
    except Exception as e:
        log.error("Error for ISBN " + isbn + ": " + str(e))
        return {
            "isbn": isbn,
            "available": False,
            "price": None,
            "title": None,
            "url": None,
            "error": str(e),
        }


def scan_all_isbns(isbns):
    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY environment variable is not set!")

    results = []
    for i, isbn in enumerate(isbns, 1):
        log.info("Scanning " + str(i) + "/" + str(len(isbns)) + ": " + isbn)
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
        return "(first scan)"
    was_available = yesterday.get("available", False)
    if not was_available and currently_available:
        return "*** NOW AVAILABLE ***"
    if was_available and not currently_available:
        return "(no longer available)"
    return ""

# ──────────────────────────────────────────────
# REPORT
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
    lines.append("Total scanned:      " + str(len(results)))
    lines.append("Momox will buy:     " + str(len(available)))
    lines.append("Momox will not buy: " + str(len(not_available)))
    lines.append("Errors:             " + str(len(errors)))
    lines.append("")

    if available:
        lines.append("MOMOX WILL BUY THESE")
        lines.append("-" * 30)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price_str = "EUR " + str(r["price"]) if r["price"] else "?"
            lines.append("  " + r["isbn"] + " | " + str(r.get("title", "?")) + " | " + price_str + " " + change)
        lines.append("")

    if not_available:
        lines.append("MOMOX WILL NOT BUY THESE TODAY")
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

    ts = "border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%'"

    if available:
        rows = make_row(["ISBN", "Title", "Price Momox pays you", "Change", "Link"], header=True)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price = "EUR " + str(r["price"]) if r["price"] else "?"
            link = '<a href="' + r["url"] + '">View on Momox</a>' if r.get("url") else ""
            rows = rows + make_row([r["isbn"], r.get("title") or "?", price, change, link])
        available_html = "<table " + ts + "><thead style='background:#e8f5e9'>" + rows + "</thead></table>"
    else:
        available_html = "<p>Momox is not buying any of your ISBNs today.</p>"

    if not_available:
        rows = make_row(["ISBN", "Title", "Change"], header=True)
        for r in not_available:
            change = get_status_change(r["isbn"], False, history)
            rows = rows + make_row([r["isbn"], r.get("title") or "?", change])
        na_html = "<table " + ts + "><thead style='background:#fdecea'>" + rows + "</thead></table>"
    else:
        na_html = "<p>None today.</p>"

    if errors:
        rows = make_row(["ISBN", "Error"], header=True)
        for r in errors:
            rows = rows + make_row([r["isbn"], r["error"]])
        err_html = (
            "<h3 style='color:orange'>Errors (" + str(len(errors)) + ")</h3>"
            "<table " + ts + "><thead style='background:#fff3e0'>" + rows + "</thead></table>"
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
        + "<h3 style='color:#c0392b'>Momox will NOT buy these today (" + str(len(not_available)) + ")</h3>"
        + na_html
        + err_html
        + "<p style='color:#aaa;font-size:12px;margin-top:30px'>Generated by Momox ISBN Agent &mdash; "
        + timestamp + "</p></body></html>"
    )

    return plain_text, html

# ──────────────────────────────────────────────
# EMAIL
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
