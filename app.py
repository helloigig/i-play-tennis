"""
🎾 Islington Tennis Court Monitor — Web App
=============================================
Flask server + Playwright scraper.
Run this, open http://localhost:5050 and you get a live dashboard.
Host it on your website / VPS and it runs 24/7.

Setup:
    pip install flask playwright apscheduler
    playwright install chromium
    python app.py
"""

import html as html_lib
import json
import re
import os
import smtplib
import threading
import time
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌  pip install playwright && playwright install chromium")
    raise

# ================================================================
# App setup
# ================================================================
app = Flask(__name__, static_folder="static", static_url_path="")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tennis")

BASE_URL = "https://bookings.better.org.uk/location/islington-tennis-centre/tennis-court-indoor"
CONFIG_PATH = Path(__file__).parent / "config.json"

# ================================================================
# State (in-memory, persisted to state.json)
# ================================================================
state = {
    "running": False,
    "slots": [],             # all slot data from last scan
    "last_check": None,
    "check_count": 0,
    "alert_count": 0,
    "alerts_log": [],        # last 50 alert events
    "activity_log": [],      # last 100 log lines
}

config = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "email_from": "",
    "email_password": "",
    "email_to": "",
    "days_ahead": 7,
    "preferred_times": [],
    "check_interval_seconds": 120,
    "alert_on_first_run": True,
    "headless": True,
    "page_load_wait_seconds": 8,
}

prev_available = {}
first_run = True
scheduler = BackgroundScheduler()
scraper_lock = threading.Lock()
pw_instance = None
browser = None
page = None


# ================================================================
# Config load / save
# ================================================================
def load_config():
    global config
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            # Strip instruction keys
            config.update({k: v for k, v in data.items() if not k.startswith("_")})
            log.info("Config loaded from config.json")
        except Exception as e:
            log.warning(f"Config load error: {e}")

    # Environment variables override config file (used for cloud deployment)
    env_map = {
        "EMAIL_FROM": "email_from",
        "EMAIL_PASSWORD": "email_password",
        "EMAIL_TO": "email_to",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config[cfg_key] = val
            log.info(f"Config: {cfg_key} set from environment")


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def save_state():
    try:
        with open(Path(__file__).parent / "state.json", "w") as f:
            json.dump({
                "slots": state["slots"],
                "last_check": state["last_check"],
                "check_count": state["check_count"],
                "alert_count": state["alert_count"],
            }, f)
    except Exception:
        pass


def load_state():
    try:
        p = Path(__file__).parent / "state.json"
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            state.update(data)
    except Exception:
        pass


# ================================================================
# Logging helper (stores in state for GUI)
# ================================================================
def add_log(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    state["activity_log"].insert(0, entry)
    state["activity_log"] = state["activity_log"][:100]
    getattr(log, level if level != "success" else "info")(msg)


# ================================================================
# Playwright browser management
# ================================================================
def ensure_browser():
    global pw_instance, browser, page
    if page and not page.is_closed():
        return page
    try:
        if browser:
            browser.close()
    except Exception:
        pass
    try:
        if pw_instance:
            pw_instance.__exit__(None, None, None)
    except Exception:
        pass

    pw_instance = sync_playwright().start()
    browser = pw_instance.chromium.launch(headless=config.get("headless", True))
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    page = ctx.new_page()
    return page


def close_browser():
    global pw_instance, browser, page
    try:
        if browser:
            browser.close()
    except Exception:
        pass
    try:
        if pw_instance:
            pw_instance.__exit__(None, None, None)
    except Exception:
        pass
    page = None
    browser = None
    pw_instance = None


# ================================================================
# Scraping
# ================================================================
def get_dates():
    today = datetime.now().date()
    return [(today + timedelta(days=i)).isoformat() for i in range(config["days_ahead"])]


def scrape_date(pg, date_str):
    url = f"{BASE_URL}/{date_str}/by-time"
    slots = []
    try:
        pg.goto(url, wait_until="domcontentloaded", timeout=20000)
        wait = config.get("page_load_wait_seconds", 8)
        try:
            pg.wait_for_selector("text=/spaces? available/i", timeout=wait * 1000)
        except Exception:
            time.sleep(wait)

        text = pg.inner_text("body")
        seen = set()
        for m in re.finditer(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text):
            t = m.group(1).zfill(5)
            if t in seen:
                continue
            seen.add(t)
            ctx = text[m.start():m.start() + 400]
            sm = re.search(r"(\d+)\s+spaces?\s+available", ctx, re.IGNORECASE)
            pm = re.search(r"£([\d.]+)", ctx)
            spaces = int(sm.group(1)) if sm else 0
            price = f"£{pm.group(1)}" if pm else "£40.00"

            # Skip past slots
            try:
                slot_dt = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %H:%M")
                if slot_dt < datetime.now():
                    continue
            except ValueError:
                pass

            slots.append({
                "date": date_str,
                "time": t,
                "end_time": m.group(2),
                "spaces": spaces,
                "price": price,
                "url": url,
            })
    except Exception as e:
        add_log(f"Error scraping {date_str}: {e}", "error")

    return sorted(slots, key=lambda s: s["time"])


# ================================================================
# Full scan cycle
# ================================================================
def run_scan():
    global prev_available, first_run

    if not scraper_lock.acquire(blocking=False):
        return  # already scanning
    try:
        _do_scan()
    finally:
        scraper_lock.release()


def _do_scan():
    global prev_available, first_run

    state["check_count"] += 1
    add_log(f"── Scan #{state['check_count']} ──")

    pg = ensure_browser()
    dates = get_dates()
    all_slots = []

    for date_str in dates:
        short = fmt_short(date_str)
        slots = scrape_date(pg, date_str)
        if slots:
            all_slots.extend(slots)
            opensl = [s for s in slots if s["spaces"] > 0]
            if opensl:
                add_log(f"{short}: {len(opensl)} open ({', '.join(s['time'] for s in opensl)})")
            else:
                add_log(f"{short}: all full")
        else:
            add_log(f"{short}: no data", "warning")
        time.sleep(1)

    state["slots"] = all_slots
    state["last_check"] = datetime.now().isoformat()

    # Detect changes
    preferred = set(config.get("preferred_times", []))
    mf = lambda s: len(preferred) == 0 or s["time"] in preferred

    current = {}
    alerts = []
    for s in all_slots:
        k = f"{s['date']}|{s['time']}"
        current[k] = s["spaces"]
        if s["spaces"] > 0 and mf(s) and prev_available.get(k, 0) == 0:
            alerts.append(s)

    prev_available = current
    total_open = sum(1 for s in all_slots if s["spaces"] > 0 and mf(s))

    # Alerts
    if first_run and config.get("alert_on_first_run") and total_open > 0:
        avail = [s for s in all_slots if s["spaces"] > 0 and mf(s)]
        send_alert_email(avail, is_summary=True)
        state["alert_count"] += 1
        add_log(f"📊 Initial scan: {total_open} slot(s) available", "success")
        first_run = False
    elif alerts:
        send_alert_email(alerts, is_summary=False)
        state["alert_count"] += len(alerts)
        state["alerts_log"].insert(0, {
            "time": datetime.now().isoformat(),
            "slots": [{"date": s["date"], "time": s["time"], "spaces": s["spaces"]} for s in alerts],
        })
        state["alerts_log"] = state["alerts_log"][:50]
        add_log(f"🎾 {len(alerts)} NEW court(s) freed up!", "success")
        first_run = False
    else:
        if first_run:
            add_log("No open slots. Will alert when something opens.")
            first_run = False
        else:
            add_log("No changes.")

    save_state()


# ================================================================
# Email
# ================================================================
def send_alert_email(slots, is_summary=False):
    if not config.get("email_from") or not config.get("email_password") or not config.get("email_to"):
        add_log("Email not configured — skipping", "warning")
        return

    n = len(slots)
    subject = (
        f"🎾 {n} court{'s' if n != 1 else ''} available this week"
        if is_summary
        else f"🎾 {n} new court{'s' if n != 1 else ''} just opened!"
    )

    by_date = {}
    for s in slots:
        by_date.setdefault(s["date"], []).append(s)

    # Plain text
    lines = []
    for d in sorted(by_date):
        lines.append(f"\n📅 {fmt_long(d)}")
        for s in sorted(by_date[d], key=lambda x: x["time"]):
            lines.append(f"   {s['time']}  —  {s['spaces']} space{'s' if s['spaces'] != 1 else ''}  {s['price']}")
    urls = sorted(set(s["url"] for s in slots))
    lines.append("\nBook now:")
    lines.extend(f"  {u}" for u in urls)
    body_text = "\n".join(lines)

    # HTML
    rows = ""
    for d in sorted(by_date):
        today_tag = ' <span style="background:#dbeafe;color:#2563eb;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">TODAY</span>' if d == datetime.now().date().isoformat() else ""
        rows += f'<tr><td colspan="4" style="padding:16px 0 8px;font-size:16px;font-weight:700;border-bottom:2px solid #d1fae5;">{fmt_long(d)}{today_tag}</td></tr>'
        for s in sorted(by_date[d], key=lambda x: x["time"]):
            badge = f'<span style="background:#d1fae5;color:#065f46;padding:3px 12px;border-radius:5px;font-weight:600;font-size:12px;">{s["spaces"]} space{"s" if s["spaces"]!=1 else ""}</span>'
            link = f'<a href="{s["url"]}" style="background:#059669;color:white;padding:6px 16px;border-radius:6px;text-decoration:none;font-weight:600;font-size:12px;display:inline-block;">Book →</a>'
            rows += f'''<tr style="border-bottom:1px solid #f0fdf4;">
              <td style="padding:10px 0;font-family:monospace;font-size:15px;font-weight:600;">{s["time"]}</td>
              <td style="padding:10px 12px;">{badge}</td>
              <td style="padding:10px 0;color:#6b7280;font-size:13px;">{s["price"]}</td>
              <td style="padding:10px 0;text-align:right;">{link}</td>
            </tr>'''

    body_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:24px;background:#fff;">
      <div style="text-align:center;padding:24px 0;border-bottom:2px solid #d1fae5;margin-bottom:16px;">
        <div style="font-size:40px;">🎾</div>
        <h1 style="font-size:22px;margin:10px 0 4px;color:#111;">{'Weekly Availability' if is_summary else 'New Courts Available!'}</h1>
        <p style="color:#6b7280;font-size:14px;margin:0;">Islington Tennis Centre — Indoor Courts</p>
      </div>
      <table style="width:100%;border-collapse:collapse;">{rows}</table>
      <div style="margin-top:28px;text-align:center;">
        <a href="{urls[0] if urls else '#'}" style="display:inline-block;background:#059669;color:white;padding:14px 40px;border-radius:10px;text-decoration:none;font-weight:700;font-size:15px;">Open Booking Page</a>
      </div>
      <p style="margin-top:28px;font-size:11px;color:#9ca3af;text-align:center;">Tennis Court Monitor · {datetime.now().strftime('%H:%M %d/%m/%Y')}</p>
    </div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = config["email_from"]
        msg["To"] = config["email_to"]
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as srv:
            srv.starttls()
            srv.login(config["email_from"], config["email_password"])
            srv.send_message(msg)
        add_log(f"📧 Email sent to {config['email_to']}", "success")
    except Exception as e:
        add_log(f"📧 Email failed: {e}", "error")


# ================================================================
# Helpers
# ================================================================
def fmt_long(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%A %-d %B")

def fmt_short(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %-d")


# ================================================================
# API Routes
# ================================================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
    preferred = set(config.get("preferred_times", []))
    mf = lambda s: len(preferred) == 0 or s["time"] in preferred
    total_open = sum(1 for s in state["slots"] if s["spaces"] > 0 and mf(s))

    return jsonify({
        "running": state["running"],
        "slots": state["slots"],
        "last_check": state["last_check"],
        "check_count": state["check_count"],
        "alert_count": state["alert_count"],
        "total_open": total_open,
        "alerts_log": state["alerts_log"][:20],
        "activity_log": state["activity_log"][:50],
        "config": {
            "email_to": config.get("email_to", ""),
            "days_ahead": config.get("days_ahead", 7),
            "preferred_times": config.get("preferred_times", []),
            "check_interval_seconds": config.get("check_interval_seconds", 120),
        },
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    # Don't expose password
    safe = {k: v for k, v in config.items() if k != "email_password"}
    safe["email_password"] = "••••••••" if config.get("email_password") else ""
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.json or {}
    for key in ["email_from", "email_to", "smtp_server", "smtp_port",
                "days_ahead", "preferred_times", "check_interval_seconds",
                "headless", "page_load_wait_seconds"]:
        if key in data:
            config[key] = data[key]
    # Only update password if it's not the masked version
    if data.get("email_password") and data["email_password"] != "••••••••":
        config["email_password"] = data["email_password"]
    save_config()

    # Restart scheduler with new interval
    if state["running"]:
        stop_monitor()
        start_monitor()

    return jsonify({"ok": True})


@app.route("/api/start", methods=["POST"])
def api_start():
    if state["running"]:
        return jsonify({"ok": True, "msg": "Already running"})
    start_monitor()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_monitor()
    return jsonify({"ok": True})


@app.route("/api/check", methods=["POST"])
def api_force_check():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/contact", methods=["POST"])
def api_contact():
    data = request.json or {}
    name    = str(data.get("name",    "")).strip()[:100]
    sender  = str(data.get("email",   "")).strip()[:200]
    skill   = str(data.get("skill",   "")).strip()[:80]
    message = str(data.get("message", "")).strip()[:2000]

    if not name or not sender or not message:
        return jsonify({"ok": False, "error": "Please fill in all required fields."}), 400

    if not config.get("email_from") or not config.get("email_password") or not config.get("email_to"):
        return jsonify({"ok": False, "error": "Email alerts are not configured on this server yet. Please reach out via social media instead."}), 503

    # Escape user content before embedding in HTML
    n = html_lib.escape(name)
    s = html_lib.escape(sender)
    k = html_lib.escape(skill) if skill else "Not specified"
    m = html_lib.escape(message)

    subject = f"🎾 Tennis Partner Request from {name}"

    body_text = (
        f"Name:    {name}\n"
        f"Email:   {sender}\n"
        f"Level:   {skill or 'Not specified'}\n\n"
        f"Message:\n{message}"
    )

    body_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:28px;background:#fff;border-radius:12px;">
      <div style="text-align:center;padding-bottom:20px;border-bottom:2px solid #d1fae5;margin-bottom:20px;">
        <div style="font-size:36px;">🎾</div>
        <h2 style="margin:10px 0 4px;color:#111;font-size:20px;">Tennis Partner Request</h2>
        <p style="color:#6b7280;font-size:13px;margin:0;">Someone wants to play!</p>
      </div>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
        <tr><td style="padding:8px 0;font-size:13px;color:#6b7280;width:80px;vertical-align:top;">Name</td><td style="padding:8px 0;font-size:14px;font-weight:600;">{n}</td></tr>
        <tr><td style="padding:8px 0;font-size:13px;color:#6b7280;vertical-align:top;">Email</td><td style="padding:8px 0;font-size:14px;"><a href="mailto:{s}" style="color:#059669;">{s}</a></td></tr>
        <tr><td style="padding:8px 0;font-size:13px;color:#6b7280;vertical-align:top;">Level</td><td style="padding:8px 0;font-size:14px;">{k}</td></tr>
      </table>
      <div style="background:#f0fdf4;border-radius:8px;padding:16px;margin-bottom:24px;">
        <p style="font-size:13px;color:#6b7280;margin:0 0 8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;">Message</p>
        <p style="font-size:14px;color:#111;white-space:pre-wrap;margin:0;line-height:1.7;">{m}</p>
      </div>
      <a href="mailto:{s}" style="display:inline-block;background:#059669;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">Reply to {n}</a>
      <p style="margin-top:24px;font-size:11px;color:#9ca3af;text-align:center;">Sent via My Tennis · {datetime.now().strftime('%H:%M %d/%m/%Y')}</p>
    </div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["From"]     = config["email_from"]
        msg["To"]       = config["email_to"]
        msg["Reply-To"] = sender
        msg["Subject"]  = subject
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as srv:
            srv.starttls()
            srv.login(config["email_from"], config["email_password"])
            srv.send_message(msg)

        add_log(f"📧 Partner request from {name} ({sender})", "success")
        return jsonify({"ok": True})
    except Exception as e:
        add_log(f"Contact form error: {e}", "error")
        return jsonify({"ok": False, "error": "Failed to send message. Please try again later."}), 500


# ================================================================
# Monitor control
# ================================================================
def start_monitor():
    global first_run
    state["running"] = True
    first_run = True
    interval = config.get("check_interval_seconds", 120)

    # Remove existing job if any
    if scheduler.get_job("scan"):
        scheduler.remove_job("scan")

    scheduler.add_job(run_scan, "interval", seconds=interval, id="scan", replace_existing=True)
    if not scheduler.running:
        scheduler.start()

    add_log(f"Monitor started — every {interval}s", "success")
    # Run first check immediately
    threading.Thread(target=run_scan, daemon=True).start()


def stop_monitor():
    state["running"] = False
    if scheduler.get_job("scan"):
        scheduler.remove_job("scan")
    close_browser()
    add_log("Monitor stopped.")


# ================================================================
# Main
# ================================================================
if __name__ == "__main__":
    load_config()
    load_state()
    save_config()  # ensure config.json exists

    # Auto-start monitor on cloud deployments
    if os.environ.get("AUTO_START", "").lower() == "true":
        log.info("AUTO_START enabled — starting monitor automatically")
        start_monitor()

    port = int(os.environ.get("PORT", 5050))
    log.info(f"🎾 Tennis Court Monitor starting on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
