"""
Momox ISBN Daily Scanner Agent
================================
Uses ScraperAPI to bypass Cloudflare protection on Momox.
Sends a daily email report of which ISBNs Momox is buying and at what price.

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
    # Add your ISBNs here, one per line, in quotes, with a comma at the end
]

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "from_email": "talherman@gmail.com",        # <- your Gmail address
    "app_password": os.environ.get("EMAIL_PASSWORD", ""),
    "to_email": "talherman@gmail.com",         # <- where to send the report
}

# ScraperAPI key — loaded from GitHub Secret (do not paste the key directly here)
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

DATA_FILE = "isbn_history.json"
DELAY_BETWEEN_REQUESTS = 2.0   # seconds between requests (saves your free quota)

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
# MOMOX SCRAPER (via ScraperAPI)
# ──────────────────────────────────────────────

def make_scraper_url(target_url):
    """Wraps any URL through ScraperAPI to bypass Cloudflare."""
    return (
        "http://api.scraperapi.com"
        "?api_key=" + SCRAPER_API_KEY +
        "&url=" + requests.utils.quote(target_url, safe="") +
        "&render=false"
    )


def check_isbn_on_momox(isbn):
    """
    Checks a single ISBN on the Momox API via ScraperAPI.
    Returns a dict with isbn, available, price, title, error.
    """
    target_url = "https://www.momox.de/api/v2/offer?ean=" + isbn

    try:
        scraper_url = make_scraper_url(target_url)
        response = requests.get(scraper_url, timeout=30)
        log.info("ISBN " + isbn + " -> HTTP " + str(response.status_code))

        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                # Not JSON — page returned HTML (unexpected)
                return {
                    "isbn": isbn,
                    "available": False,
                    "price": None,
                    "title": None,
                    "url": "https://www.momox.de/suche/?q=" + isbn,
                    "error": "Unexpected non-JSON response",
                }

            price = data.get("price") or data.get("purchasePrice")
            title = data.get("title") or data.get("name") or "Unknown title"

            if price is not None:
                try:
                    price_float = float(str(price).replace(",", "."))
                    available = price_float > 0
                except ValueError:
                    available = False
            else:
                available = False

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
                "error": "Not found on Momox",
            }

        elif response.status_code == 403:
            return {
                "isbn": isbn,
                "available": False,
                "price": None,
                "title": None,
                "url": None,
                "error": "Still blocked (403) - check your ScraperAPI key",
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

    except requests.exceptions.Timeout:
        return {
            "isbn": isbn,
            "available": False,
            "price": None,
            "title": None,
            "url": None,
            "error": "Request timed out",
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
    """Scan all ISBNs one by one with a delay to preserve quota."""
    if not SCRAPER_API_KEY:
        log.error("SCRAPER_API_KEY is not set! Add it as a GitHub Secret.")
        raise ValueError("Missing SCRAPER_API_KEY environment variable.")

    results = []
    for i, isbn in enumerate(isbns, 1):
        log.info("Scanning " + str(i) + "/" + str(len(isbns)) + ": ISBN " + isbn)
        result = check_isbn_on_momox(isbn)
        results.append(result)
        if i < len(isbns):
            time.sleep(DELAY_BETWEEN_REQUESTS)
    return results

# ──────────────────────────────────────────────
# HISTORY (tracks changes day to day)
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
        return "*** NOW AVAILABLE - was not yesterday ***"
    if was_available and not currently_available:
        return "(no longer available - was available yesterday)"
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

    # Plain text version
    lines = []
    lines.append("Momox ISBN Daily Report - " + today)
    lines.append("=" * 50)
    lines.append("Total scanned:          " + str(len(results)))
    lines.append("Momox will buy:         " + str(len(available)))
    lines.append("Momox will not buy:     " + str(len(not_available)))
    lines.append("Errors:                 " + str(len(errors)))
    lines.append("")

    if available:
        lines.append("MOMOX WILL BUY THESE")
        lines.append("-" * 30)
        for r in available:
            change = get_status_change(r["isbn"], True, history)
            price_str = "EUR " + str(r["price"]) if r["price"] else "price unknown"
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

    # HTML version
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
        + "<p style='color:#aaa;font-size:12px;margin-top:30px'>"
        + "Generated by Momox ISBN Agent &mdash; " + timestamp
        + "</p></body></html>"
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
    log.info("Scanning " + str(len(ISBNS)) + " ISBNs via ScraperAPI -> Momox...")

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
