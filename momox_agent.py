"""
Momox ISBN Daily Scanner Agent
================================
Uses ScraperAPI to check Momox prices for a list of ISBNs.
Correct URL format: https://www.momox.de/offer/{ISBN}
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
    "9783426282731", # Schreib oder Stirb - Sebastian Fitzek
    "9783446254411", # Ellbogen - Fatma Aydemir
    "9783596196944", # Ostfriesenzorn - Klaus-Peter Wolf
    "9783446260139", # Finsterwald - Christian Tielmann
    "9783442439157", # Vergessene Welt - Michael Crichton
    "9783492314541", # Wisting und der Tag der Toten - Jørn Lier Horst
    "9783462048285", # Ach, diese Lücke, diese entsetzliche Lücke - Joachim Meyerhoff
    "9783740801366", # Alstersturm - Bodo Manstein
    "9783426519189", # Der letzte Atemzug - Gabriella Ullberg Westin
    "9783442035229"  # Das Medaillon - C.C. Bergius
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
    # Correct URL format discovered from real browser
    target_url = "https://www.momox.de/offer/" + isbn

    # Strategy 1: plain fetch (no JS) — fast and cheap
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "render": "false",
        "country_code": "de",
    }

    try:
        log.info("Trying plain fetch for " + isbn)
        response = requests.get("https://api.scraperapi.com/", params=params, timeout=60)
        log.info("HTTP " + str(response.status_code) + " — " + str(len(response.text)) + " chars")

        if response.status_code == 200:
            html = response.text

            # Log a preview to help debug
            log.info("Preview: " + html[:300])

            result = parse_momox_page(isbn, html, target_url)
            if result is not None:
                return result

        # Strategy 2: JS rendered — slower but more complete
        log.info("Plain fetch inconclusive, trying JS render for " + isbn)
        params["render"] = "true"
        response = requests.get("https://api.scraperapi.com/", params=params, timeout=120)
        log.info("JS render HTTP " + str(response.status_code) + " — " + str(len(response.text)) + " chars")

        if response.status_code == 200:
            html = response.text
            log.info("JS Preview: " + html[:300])

            result = parse_momox_page(isbn, html, target_url)
            if result is not None:
                return result

        return {
            "isbn": isbn, "available": False, "price": None, "title": None,
            "url": target_url,
            "error": "Could not parse price — HTTP " + str(response.status_code),
        }

    except requests.exceptions.Timeout:
        return {
            "isbn": isbn, "available": False, "price": None, "title": None,
            "url": target_url, "error": "Timed out",
        }
    except Exception as e:
        log.error("Error for " + isbn + ": " + str(e))
        return {
            "isbn": isbn, "available": False, "price": None, "title": None,
            "url": target_url, "error": str(e),
        }


def parse_momox_page(isbn, html, url):
    """
    Parse price and title from a Momox offer page.
    Returns a result dict if successful, or None if inconclusive.
    """

    # Check "not buying" signals first
    not_buying_signals = [
        "leider nicht ankaufen",
        "nicht angekauft",
        "wird leider nicht",
        "no_offer",
        "not_accepted",
    ]
    for signal in not_buying_signals:
        if signal.lower() in html.lower():
            log.info("Found not-buying signal: " + signal)
            title = extract_title(html, isbn)
            return {
                "isbn": isbn, "available": False, "price": None,
                "title": title, "url": url, "error": None,
            }

    # Try to find price in JSON data embedded in the page
    price = None
    title = None

    # Look for JSON blobs containing price
    json_blobs = re.findall(r'\{[^{}]{0,2000}\}', html)
    for blob in json_blobs:
        if "price" not in blob.lower() and "ankauf" not in blob.lower():
            continue
        try:
            data = json.loads(blob)
            for key in ["price", "purchasePrice", "sell_price", "ankaufspreis", "offer_price"]:
                val = data.get(key)
                if val is not None:
                    try:
                        pf = float(str(val).replace(",", "."))
                        if 0 < pf < 500:
                            price = str(round(pf, 2))
                            log.info("Found price in JSON blob: " + price)
                            title = data.get("title") or data.get("name") or extract_title(html, isbn)
                            break
                    except ValueError:
                        pass
            if price:
                break
        except Exception:
            continue

    # Look for "Du erhältst X,XX €" — the buyback price text shown on the page
    if not price:
        match = re.search(
            r'Du\s+erh[äa]ltst.{0,200}?(\d{1,3}[,\.]\d{2})\s*\u20ac',
            html, re.IGNORECASE | re.DOTALL
        )
        if match:
            try:
                pf = float(match.group(1).replace(",", "."))
                if 0 < pf < 500:
                    price = str(round(pf, 2))
                    log.info("Found price via Du-erhaeltst: " + price)
            except ValueError:
                pass

    # Look for any price near the top of the page (before footer)
    if not price:
        footer_pos = html.lower().find("<footer")
        html_main = html[:footer_pos] if footer_pos > 0 else html
        # Match patterns like "1,64 €" or "1.64€"
        matches = re.findall(r'(\d{1,3}[,\.]\d{2})\s*\u20ac', html_main)
        for m in matches:
            try:
                pf = float(m.replace(",", "."))
                # Avoid suspiciously round numbers that are likely generic (e.g. 5.25)
                if 0 < pf < 500 and pf != 5.25:
                    price = str(round(pf, 2))
                    log.info("Found price via generic pattern: " + price)
                    break
            except ValueError:
                pass

    if not title:
        title = extract_title(html, isbn)

    log.info("parse result — price: " + str(price) + ", title: " + str(title))

    if price:
        return {
            "isbn": isbn, "available": True, "price": price,
            "title": title, "url": url, "error": None,
        }

    # Return None = inconclusive, caller will try next strategy
    return None


def extract_title(html, isbn):
    """Try to extract the book title from various places in the HTML."""
    # JSON-LD structured data
    jsonld = re.search(r'application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if jsonld:
        try:
            jd = json.loads(jsonld.group(1))
            t = jd.get("name") or jd.get("title")
            if t:
                return t
        except Exception:
            pass

    # <title> tag (but skip generic ones)
    title_match = re.search(r'<title>([^<]+)</title>', html)
    if title_match:
        t = title_match.group(1).strip()
        if "momox" not in t.lower() and len(t) > 5:
            return t

    # <h1> tag
    h1_match = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    if h1_match:
        t = h1_match.group(1).strip()
        if len(t) > 3:
            return t

    return isbn  # fallback to ISBN if no title found


def scan_all_isbns(isbns):
    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY environment variable is not set!")
    results = []
    for i, isbn in enumerate(isbns, 1):
        log.info("=== " + str(i) + "/" + str(len(isbns)) + ": " + isbn + " ===")
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
