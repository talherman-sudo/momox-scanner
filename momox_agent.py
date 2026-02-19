"""
Momox ISBN Daily Scanner Agent
================================
Uses ScraperAPI to check Momox prices for a list of ISBNs.
Setup: pip install requests
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
    # Add your ISBNs here
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
DELAY_BETWEEN_REQUESTS = 4.0

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

def scraper_get(target_url, render=False):
    """Make a request through ScraperAPI. Returns response or None."""
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "true" if render else "false",
        "country_code": "de",
    }
    try:
        response = requests.get(
            "https://api.scraperapi.com/",
            params=params,
            timeout=90,
        )
        log.info("GET " + target_url + " -> HTTP " + str(response.status_code))
        return response
    except Exception as e:
        log.error("Request error: " + str(e))
        return None


def check_isbn_on_momox(isbn):
    """
    Try multiple strategies to get the Momox buy price for an ISBN.
    """

    # ── Strategy 1: Try plain (no JS) fetch of the search page ──
    # Momox embeds product data as JSON inside a <script> tag in the HTML
    response = scraper_get("https://www.momox.de/?search=" + isbn, render=False)

    if response and response.status_code == 200:
        html = response.text
        log.info("Strategy 1 HTML length: " + str(len(html)))

        # Look for embedded JSON data in script tags (Next.js / React apps do this)
        # Pattern: __NEXT_DATA__ or similar
        json_matches = re.findall(
            r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        for jtext in json_matches:
            try:
                jdata = json.loads(jtext)
                jstr = json.dumps(jdata)
                # Look for price in the JSON blob
                price_match = re.search(r'"(?:price|purchasePrice|ankaufspreis|sell_price)"\s*:\s*"?(\d+[\.,]\d{2})"?', jstr)
                if price_match:
                    price_str = price_match.group(1).replace(",", ".")
                    price_float = float(price_str)
                    if 0 < price_float < 500:
                        title_match = re.search(r'"(?:title|name)"\s*:\s*"([^"]{5,100})"', jstr)
                        title = title_match.group(1) if title_match else "Unknown"
                        log.info("Found price in embedded JSON: " + str(price_float))
                        return {
                            "isbn": isbn, "available": True,
                            "price": str(price_float), "title": title,
                            "url": "https://www.momox.de/?search=" + isbn, "error": None,
                        }
            except Exception:
                continue

        # Look for NEXT_DATA script
        next_match = re.search(r'id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
        if next_match:
            try:
                jdata = json.loads(next_match.group(1))
                jstr = json.dumps(jdata)
                price_match = re.search(r'"(?:price|purchasePrice|sell_price)"\s*:\s*"?(\d+[\.,]\d{2})"?', jstr)
                if price_match:
                    price_str = price_match.group(1).replace(",", ".")
                    price_float = float(price_str)
                    if 0 < price_float < 500:
                        log.info("Found price in __NEXT_DATA__: " + str(price_float))
                        return {
                            "isbn": isbn, "available": True,
                            "price": str(price_float), "title": isbn,
                            "url": "https://www.momox.de/?search=" + isbn, "error": None,
                        }
            except Exception:
                pass

        # Check for "not buying" signals even if no price found
        not_buying = any(s in html.lower() for s in [
            "leider nicht ankaufen", "nicht angekauft",
            "wird leider nicht", "no_offer"
        ])
        if not_buying:
            return {
                "isbn": isbn, "available": False, "price": None, "title": None,
                "url": "https://www.momox.de/?search=" + isbn, "error": None,
            }

    # ── Strategy 2: JS-rendered page, simple params only ──
    log.info("Strategy 1 found no price, trying JS render for " + isbn)
    response = scraper_get("https://www.momox.de/?search=" + isbn, render=True)

    if response and response.status_code == 200:
        html = response.text
        log.info("Strategy 2 HTML length: " + str(len(html)))

        # Remove footer to avoid picking up unrelated prices
        footer_pos = html.lower().find("<footer")
        if footer_pos > 0:
            html = html[:footer_pos]

        # Find "Du erhältst X,XX €" — the exact buyback price text
        match = re.search(
            r'Du\s+erh[äa]ltst.{0,300}?(\d{1,3}[,\.]\d{2})\s*\u20ac',
            html, re.IGNORECASE | re.DOTALL
        )
        if match:
            price_str = match.group(1).replace(",", ".")
            try:
                price_float = float(price_str)
                if 0 < price_float < 500:
                    log.info("Found price via Du-erhaeltst: " + str(price_float))

                    # Try to get title from JSON-LD
                    title = isbn
                    jsonld = re.search(
                        r'application/ld\+json["\'][^>]*>(.*?)</script>',
                        html, re.DOTALL
                    )
                    if jsonld:
                        try:
                            jd = json.loads(jsonld.group(1))
                            title = jd.get("name") or jd.get("title") or isbn
                        except Exception:
                            pass

                    return {
                        "isbn": isbn, "available": True,
                        "price": str(price_float), "title": title,
                        "url": "https://www.momox.de/?search=" + isbn, "error": None,
                    }
            except ValueError:
                pass

        # Check not-buying signals
        not_buying = any(s in html.lower() for s in [
            "leider nicht ankaufen", "nicht angekauft",
            "wird leider nicht", "no_offer"
        ])
        if not_buying:
            return {
                "isbn": isbn, "available": False, "price": None, "title": None,
                "url": "https://www.momox.de/?search=" + isbn, "error": None,
            }

        # Could not determine — return error with note to check manually
        log.warning("Could not find price or not-buying signal for " + isbn)
        return {
            "isbn": isbn, "available": False, "price": None, "title": None,
            "url": "https://www.momox.de/?search=" + isbn,
            "error": "Could not parse price — check manually at momox.de/?search=" + isbn,
        }

    # Both strategies failed
    status = str(response.status_code) if response else "no response"
    return {
        "isbn": isbn, "available": False, "price": None, "title": None,
        "url": None, "error": "Failed (HTTP " + status + ")",
    }


def scan_all_isbns(isbns):
    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY environment variable is not set!")
    results = []
    for i, isbn in enumerate(isbns, 1):
        log.info("=== Scanning " + str(i) + "/" + str(len(isbns)) + ": " + isbn + " ===")
        result = check_isbn_on_momox(isbn)
        log.info("Result: " + str(result))
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
        + na_html + err_html
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
