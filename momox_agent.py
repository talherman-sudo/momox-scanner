"""
Momox ISBN Weekly Scanner Agent
================================
Optimized to minimize ScraperAPI credits:
1. Tries direct API endpoint first (1 credit)
2. Falls back to plain HTML fetch (1 credit)
3. Only uses JS rendering as last resort (10 credits)
4. Remembers which strategy worked per ISBN to be smarter next time

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
    "9783437423963",
    "3194245031",
    "3437425064",
    "343742159X",
    "3437421786",
    "3540765115",
    "3938509449",
    "3540417613",
    "3437431315",
    "3929851369",
    "3527708413",
    "386026172X",
    "241491517",
    "3642123767",
    "3860261711",
    "3860261819",
    "3442314879",
    "354064394X",
    "343742534X",
    "3437413031",
    "3898839729",
    "3868920226",
    "3499623862",
    "3499249413",
    "3548363938",
    "3499626519",
    "3440108430",
    "9783868699715"
]

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "from_email": "talherman@gmail.com",
    "app_password": os.environ.get("EMAIL_PASSWORD", ""),
    "to_email": "talherman@gmail.com,nadav.herman.nh@gmail.com",
}

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
DATA_FILE = "isbn_history.json"
METHODS_FILE = "isbn_methods.json"  # remembers cheapest working strategy per ISBN
DELAY_BETWEEN_REQUESTS = 2.0

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
# METHODS CACHE (remembers cheapest strategy per ISBN)
# ──────────────────────────────────────────────

def load_methods():
    if os.path.exists(METHODS_FILE):
        with open(METHODS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_methods(methods):
    with open(METHODS_FILE, "w") as f:
        json.dump(methods, f, indent=2)

# ──────────────────────────────────────────────
# SCRAPERAPI HELPER
# ──────────────────────────────────────────────

def scraper_get(url, render=False):
    """
    Make a request through ScraperAPI.
    render=False = 1 credit, render=True = 10 credits.
    """
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "render": "true" if render else "false",
        "country_code": "de",
    }
    try:
        response = requests.get(
            "https://api.scraperapi.com/",
            params=params,
            timeout=90 if not render else 120,
        )
        cost = "10 credits" if render else "1 credit"
        log.info("GET " + url + " -> HTTP " + str(response.status_code) + " (" + cost + ")")
        return response
    except Exception as e:
        log.error("Request error: " + str(e))
        return None

# ──────────────────────────────────────────────
# PRICE PARSING
# ──────────────────────────────────────────────

def parse_price_from_json(data):
    """Extract price from a parsed JSON dict."""
    for key in ["price", "purchasePrice", "sell_price", "ankaufspreis", "offer_price"]:
        val = data.get(key)
        if val is not None:
            try:
                pf = float(str(val).replace(",", "."))
                if 0 < pf < 500:
                    return str(round(pf, 2))
            except ValueError:
                pass
    return None


def parse_price_from_html(html):
    """
    Extract price from HTML page.
    Strips footer first to avoid false positives.
    Only looks for 'Du erhältst X,XX €' — the real buyback price.
    """
    # Strip footer
    footer_pos = html.lower().find("<footer")
    html_main = html[:footer_pos] if footer_pos > 0 else html

    # Most reliable: "Du erhältst X,XX €"
    match = re.search(
        r'Du\s+erh[äa]ltst.{0,200}?(\d{1,3}[,\.]\d{2})\s*\u20ac',
        html_main, re.IGNORECASE | re.DOTALL
    )
    if match:
        try:
            pf = float(match.group(1).replace(",", "."))
            if 0 < pf < 500:
                return str(round(pf, 2))
        except ValueError:
            pass

    # Second attempt: find price in embedded JSON blobs
    for blob in re.findall(r'\{[^{}]{0,1000}\}', html_main):
        if "price" not in blob.lower():
            continue
        try:
            data = json.loads(blob)
            price = parse_price_from_json(data)
            if price and price != "5.25":  # exclude known false positive
                return price
        except Exception:
            continue

    return None


def extract_title(html, isbn):
    """Try to extract book title from HTML."""
    # JSON-LD structured data is most reliable
    jsonld = re.search(r'application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if jsonld:
        try:
            jd = json.loads(jsonld.group(1))
            t = jd.get("name") or jd.get("title")
            if t and len(t) > 3:
                return t
        except Exception:
            pass
    # <h1> tag
    h1 = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    if h1:
        t = h1.group(1).strip()
        if len(t) > 3 and "momox" not in t.lower():
            return t
    return isbn


def is_not_buying(html):
    """Check if Momox explicitly says they won't buy this item."""
    signals = ["leider nicht ankaufen", "nicht angekauft", "wird leider nicht", "no_offer"]
    return any(s in html.lower() for s in signals)

# ──────────────────────────────────────────────
# MAIN ISBN CHECKER — 3 strategies, cheapest first
# ──────────────────────────────────────────────

def check_isbn_on_momox(isbn, known_method=None):
    """
    Try strategies in order of cost, starting with the known working one.
    Returns (result_dict, method_that_worked)
    """
    offer_url = "https://www.momox.de/offer/" + isbn
    api_url = "https://www.momox.de/api/v4/offer/?ean=" + isbn

    # If we know which method worked last time, start there
    strategies = ["api", "plain", "render"]
    if known_method and known_method in strategies:
        # Put known method first, keep others as fallback
        strategies = [known_method] + [s for s in strategies if s != known_method]

    for strategy in strategies:
        log.info("Trying strategy '" + strategy + "' for ISBN " + isbn)

        # ── Strategy: direct API (1 credit, JSON response) ──
        if strategy == "api":
            response = scraper_get(api_url, render=False)
            if response and response.status_code == 200:
                text = response.text.strip()
                if not text.startswith("<"):  # got JSON not HTML
                    try:
                        data = response.json()
                        price = parse_price_from_json(data)
                        status = data.get("status", "")
                        if "no_offer" in str(status).lower() or "blocked" in str(status).lower():
                            title = data.get("title") or data.get("name") or isbn
                            return {"isbn": isbn, "available": False, "price": None,
                                    "title": title, "url": offer_url, "error": None}, "api"
                        if price:
                            title = data.get("title") or data.get("name") or isbn
                            log.info("API strategy succeeded: EUR " + price)
                            return {"isbn": isbn, "available": True, "price": price,
                                    "title": title, "url": offer_url, "error": None}, "api"
                    except Exception as e:
                        log.info("API JSON parse failed: " + str(e))

        # ── Strategy: plain HTML fetch (1 credit) ──
        elif strategy == "plain":
            response = scraper_get(offer_url, render=False)
            if response and response.status_code == 200:
                html = response.text
                if is_not_buying(html):
                    return {"isbn": isbn, "available": False, "price": None,
                            "title": extract_title(html, isbn), "url": offer_url, "error": None}, "plain"
                price = parse_price_from_html(html)
                if price:
                    log.info("Plain strategy succeeded: EUR " + price)
                    return {"isbn": isbn, "available": True, "price": price,
                            "title": extract_title(html, isbn), "url": offer_url, "error": None}, "plain"

        # ── Strategy: JS rendered (10 credits — last resort) ──
        elif strategy == "render":
            log.info("Using JS render (10 credits) for " + isbn)
            response = scraper_get(offer_url, render=True)
            if response and response.status_code == 200:
                html = response.text
                if is_not_buying(html):
                    return {"isbn": isbn, "available": False, "price": None,
                            "title": extract_title(html, isbn), "url": offer_url, "error": None}, "render"
                price = parse_price_from_html(html)
                if price:
                    log.info("Render strategy succeeded: EUR " + price)
                    return {"isbn": isbn, "available": True, "price": price,
                            "title": extract_title(html, isbn), "url": offer_url, "error": None}, "render"
            status = str(response.status_code) if response else "no response"
            log.warning("All strategies failed for " + isbn)
            return {"isbn": isbn, "available": False, "price": None, "title": isbn,
                    "url": offer_url, "error": "All strategies failed (last HTTP: " + status + ")"}, None

    # Exhausted all strategies
    return {"isbn": isbn, "available": False, "price": None, "title": isbn,
            "url": offer_url, "error": "Could not retrieve data"}, None


def scan_all_isbns(isbns):
    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY is not set!")

    methods = load_methods()
    results = []
    total_credits = 0

    # Pass 1: try cheap strategies (api + plain) for all ISBNs
    pending_render = []  # ISBNs that need JS rendering

    for i, isbn in enumerate(isbns, 1):
        log.info("=== " + str(i) + "/" + str(len(isbns)) + ": " + isbn + " ===")
        known = methods.get(isbn)

        # If last time needed JS render, skip cheap attempts and go straight to render
        if known == "render":
            pending_render.append(isbn)
            continue

        result, method = check_isbn_on_momox(isbn, known_method=known)

        if method == "render":
            # Shouldn't happen in pass 1, but handle it
            total_credits += 10
        elif method in ("api", "plain"):
            total_credits += 1
        else:
            # All cheap methods failed, queue for JS render
            pending_render.append(isbn)
            continue

        if method:
            methods[isbn] = method
        results.append(result)

        if i < len(isbns):
            time.sleep(DELAY_BETWEEN_REQUESTS)

    # Pass 2: JS render only for ISBNs that need it
    if pending_render:
        log.info("=== Pass 2: JS rendering " + str(len(pending_render)) + " ISBNs ===")
        for i, isbn in enumerate(pending_render, 1):
            log.info("JS render " + str(i) + "/" + str(len(pending_render)) + ": " + isbn)
            result, method = check_isbn_on_momox(isbn, known_method="render")
            total_credits += 10
            if method:
                methods[isbn] = method
            results.append(result)
            if i < len(pending_render):
                time.sleep(DELAY_BETWEEN_REQUESTS)

    save_methods(methods)
    log.info("=== Total estimated credits used: ~" + str(total_credits) + " ===")
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
    lines.append("Momox ISBN Weekly Report - " + today)
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
        lines.append("MOMOX WILL NOT BUY THESE")
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
        available_html = "<p>Momox is not buying any of your ISBNs this week.</p>"

    if not_available:
        rows = make_row(["ISBN", "Title", "Change"], header=True)
        for r in not_available:
            change = get_status_change(r["isbn"], False, history)
            rows = rows + make_row([r["isbn"], r.get("title") or "?", change])
        na_html = "<table " + ts + "><thead style='background:#fdecea'>" + rows + "</thead></table>"
    else:
        na_html = "<p>None this week.</p>"

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
        "<h2 style='color:#333'>Momox ISBN Weekly Report</h2>"
        "<p style='color:#666'>" + today + " &mdash; " + str(len(results)) + " ISBNs scanned</p>"
        "<h3 style='color:green'>Momox will BUY these (" + str(len(available)) + ")</h3>"
        + available_html
        + "<h3 style='color:#c0392b'>Momox will NOT buy these (" + str(len(not_available)) + ")</h3>"
        + na_html + err_html
        + "<p style='color:#aaa;font-size:12px;margin-top:30px'>Generated by Momox ISBN Agent &mdash; "
        + timestamp + "</p></body></html>"
    )
    return plain_text, html

# ──────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────

def send_email(plain_text, html, config):
    recipients = [r.strip() for r in config["to_email"].split(",")]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Momox ISBN Report - " + str(date.today())
    msg["From"] = config["from_email"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL(config["smtp_server"], config["smtp_port"]) as server:
        server.login(config["from_email"], config["app_password"])
        server.sendmail(config["from_email"], recipients, msg.as_string())
    log.info("Report emailed to " + str(recipients))

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
