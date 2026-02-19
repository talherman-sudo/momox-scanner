"""
Momox ISBN Daily Scanner Agent
================================
Uses ScraperAPI to fetch Momox prices for a list of ISBNs.
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

def check_isbn_on_momox(isbn):
    """
    Strategy: use the Momox JSON API endpoint directly via ScraperAPI.
    The endpoint /api/v4/media/sell/ returns structured JSON with price and status.
    """

    # Try the structured JSON endpoint first — much more reliable than HTML parsing
    endpoints_to_try = [
        "https://www.momox.de/api/v4/media/sell/?ean=" + isbn,
        "https://www.momox.de/api/v3/sell/?ean=" + isbn,
        "https://www.momox.de/api/sell/?ean=" + isbn,
    ]

    for endpoint in endpoints_to_try:
        params = {
            "api_key": SCRAPER_API_KEY,
            "url": endpoint,
            "render": "false",
            "country_code": "de",
            "keep_headers": "true",
        }
        headers = {
            "Accept": "application/json",
            "Accept-Language": "de-DE,de;q=0.9",
            "Referer": "https://www.momox.de/",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            response = requests.get(
                "https://api.scraperapi.com/",
                params=params,
                headers=headers,
                timeout=60,
            )
            log.info("Endpoint " + endpoint + " -> HTTP " + str(response.status_code))

            if response.status_code == 200:
                text = response.text.strip()
                log.info("Response: " + text[:300])

                # Skip if we got HTML instead of JSON
                if text.startswith("<"):
                    log.info("Got HTML, skipping to next endpoint")
                    continue

                try:
                    data = response.json()
                except Exception:
                    log.info("Could not parse JSON, skipping")
                    continue

                # Extract price and status from JSON
                price = (data.get("price") or
                         data.get("purchasePrice") or
                         data.get("sell_price") or
                         data.get("ankaufspreis"))
                status = data.get("status") or data.get("state") or ""
                title = (data.get("title") or
                         data.get("name") or
                         data.get("product", {}).get("title") if isinstance(data.get("product"), dict) else None or
                         "Unknown title")

                # Check if Momox explicitly says no
                not_buying = any(s in str(status).lower() for s in ["no_offer", "blocked", "not_accepted", "kein"])
                if not_buying:
                    return {
                        "isbn": isbn,
                        "available": False,
                        "price": None,
                        "title": title,
                        "url": "https://www.momox.de/?search=" + isbn,
                        "error": None,
                    }

                if price is not None:
                    try:
                        price_float = float(str(price).replace(",", "."))
                        if price_float > 0:
                            return {
                                "isbn": isbn,
                                "available": True,
                                "price": str(price_float),
                                "title": title,
                                "url": "https://www.momox.de/?search=" + isbn,
                                "error": None,
                            }
                    except ValueError:
                        pass

        except Exception as e:
            log.warning("Error with endpoint " + endpoint + ": " + str(e))
            continue

    # All JSON endpoints failed — fall back to HTML page with very targeted parsing
    log.info("JSON endpoints failed, falling back to HTML parsing for " + isbn)
    return check_isbn_via_html(isbn)


def check_isbn_via_html(isbn):
    """
    Fallback: fetch the rendered Momox page and extract price very carefully.
    Only looks in the product result area, NOT the footer.
    """
    target_url = "https://www.momox.de/?search=" + isbn

    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "true",
        "country_code": "de",
        "wait_for_selector": ".sell-offer, .price, [class*='price'], [class*='offer']",
        "wait": "3000",   # wait 3 seconds for JS to load
    }

    try:
        response = requests.get(
            "https://api.scraperapi.com/",
            params=params,
            timeout=120,
        )
        log.info("HTML fallback for " + isbn + " -> HTTP " + str(response.status_code))

        if response.status_code != 200:
            return {
                "isbn": isbn, "available": False, "price": None, "title": None,
                "url": "https://www.momox.de/?search=" + isbn,
                "error": "HTTP " + str(response.status_code),
            }

        html = response.text
        log.info("HTML length: " + str(len(html)))

        # Cut out the footer to avoid false positives
        # Find the main content area before the footer
        footer_pos = html.lower().find("<footer")
        if footer_pos > 0:
            html_main = html[:footer_pos]
            log.info("Trimmed HTML to " + str(len(html_main)) + " chars (removed footer)")
        else:
            html_main = html

        # Also cut out the header/nav area if possible
        main_pos = html_main.lower().find("<main")
        if main_pos > 0:
            html_main = html_main[main_pos:]

        log.info("Main content preview: " + html_main[:500])

        # Look specifically for "Du erhältst" followed by a price — this is the buyback price
        price = None
        title = None

        # Very specific pattern: "Du erhältst" then price within 200 chars
        erhaeltst_match = re.search(
            r'Du\s+erh[äa]ltst.{0,200}?(\d{1,3}[,\.]\d{2})\s*[€E]',
            html_main, re.IGNORECASE | re.DOTALL
        )
        if erhaeltst_match:
            price_str = erhaeltst_match.group(1).replace(",", ".")
            try:
                price_float = float(price_str)
                if 0 < price_float < 500:   # sanity check
                    price = str(price_float)
                    log.info("Found price via Du-erhaeltst pattern: " + price)
            except ValueError:
                pass

        # Look for title in JSON-LD structured data (most reliable)
        jsonld_match = re.search(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
        if jsonld_match:
            try:
                jdata = json.loads(jsonld_match.group(1))
                title = jdata.get("name") or jdata.get("title")
            except Exception:
                pass

        # Not buying signals
        not_buying_signals = ["leider nicht ankaufen", "nicht angekauft", "wird leider nicht", "no_offer"]
        for signal in not_buying_signals:
            if signal.lower() in html.lower():
                return {
                    "isbn": isbn, "available": False, "price": None, "title": title,
                    "url": "https://www.momox.de/?search=" + isbn, "error": None,
                }

        available = price is not None
        return {
            "isbn": isbn,
            "available": available,
            "price": price,
            "title": title,
            "url": "https://www.momox.de/?search=" + isbn,
            "error": None if available else "Could not find price — may need manual check",
        }

    except requests.exceptions.Timeout:
        return {
            "isbn": isbn, "available": False, "price": None, "title": None,
            "url": None, "error": "Timed out",
        }
    except Exception as e:
        return {
            "isbn": isbn, "available": False, "price": None, "title": None,
            "url": None, "error": str(e),
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
