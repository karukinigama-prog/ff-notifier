"""
╔══════════════════════════════════════════════════════════════════╗
║       FREE FIRE REDEEM CODE NOTIFIER — Firebase FCM Edition      ║
║       Backend Server — Runs 24/7 on PythonAnywhere / Render      ║
╠══════════════════════════════════════════════════════════════════╣
║  INSTALL DEPENDENCIES:                                           ║
║  pip install requests playwright groq beautifulsoup4 lxml        ║
║             firebase-admin                                       ║
║  playwright install chromium                                     ║
╠══════════════════════════════════════════════════════════════════╣
║  FILES NEEDED IN THE SAME FOLDER:                                ║
║    • server.py              ← this file                          ║
║    • serviceAccountKey.json ← downloaded from Firebase Console   ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ──────────────────────────────────────────────────────────────────
#  CONFIGURATION  ← only edit this block
# ──────────────────────────────────────────────────────────────────
GROQ_API_KEY            = "YOUR_GROQ_API_KEY_HERE"      # https://console.groq.com/keys
SERVICE_ACCOUNT_PATH    = "serviceAccountKey.json"      # path to your Firebase service account file
FCM_TOPIC               = "ff_codes"                    # mobile app subscribes to this topic
CHECK_INTERVAL_SECONDS  = 300                           # 5 minutes between scrape cycles
# ──────────────────────────────────────────────────────────────────

import time
import json
import sqlite3
import logging
import re
import os
import requests
from datetime import datetime

from groq import Groq
import firebase_admin
from firebase_admin import credentials, messaging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

# ─── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("ff_notifier.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────
DB_PATH    = "sent_codes.db"
REDEEM_URL = "https://reward.ff.garena.com/"

SOURCES = [
    (
        "FF-Garena News",
        "https://ff.garena.com/news/",
        "article, .news-item, .post-content, p",
    ),
    (
        "Reddit r/freefire (new)",
        "https://www.reddit.com/r/freefire/new/.json?limit=15&t=day",
        None,  # JSON endpoint
    ),
    (
        "Mejoress FF Codes",
        "https://www.mejoress.com/en/free-fire-codes/",
        "article, .entry-content, p, li",
    ),
]

GROQ_MODEL  = "llama3-8b-8192"
GROQ_PROMPT = """
You are a code-extraction assistant for the mobile game Garena Free Fire.
Analyze the text below and look for any redeem/reward codes.
Free Fire codes are exactly 12 or 16 alphanumeric characters (A-Z, 0-9), uppercase.
They appear after words like "code", "redeem", "reward", or in bold/backtick formatting.

Return ONLY a valid JSON object — no markdown, no extra text:
{{"has_code": true, "code": "THE_CODE", "reward": "Description of reward or empty string"}}

If NO valid code is found:
{{"has_code": false, "code": "", "reward": ""}}

Text to analyze:
\"\"\"
{text}
\"\"\"
"""

# ─── Firebase Initialisation ─────────────────────────────────────

def init_firebase() -> None:
    """Initialise the Firebase Admin SDK once at startup."""
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise FileNotFoundError(
            f"serviceAccountKey.json not found at '{SERVICE_ACCOUNT_PATH}'.\n"
            "Download it from: Firebase Console → Project Settings → Service Accounts."
        )
    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred)
    log.info("Firebase Admin SDK initialised ✅")


# ─── SQLite Database ─────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_codes (
            code     TEXT PRIMARY KEY,
            reward   TEXT,
            source   TEXT,
            sent_at  TEXT
        )
    """)
    conn.commit()
    log.info("SQLite database ready at %s", DB_PATH)
    return conn


def is_already_sent(conn: sqlite3.Connection, code: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sent_codes WHERE code = ?", (code,)
    ).fetchone() is not None


def mark_as_sent(conn: sqlite3.Connection, code: str, reward: str, source: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sent_codes (code, reward, source, sent_at) VALUES (?,?,?,?)",
        (code, reward, source, datetime.utcnow().isoformat()),
    )
    conn.commit()


# ─── Web Scraping ────────────────────────────────────────────────

def scrape_with_playwright(url: str, css_selector: str) -> str:
    """Headless Chromium scrape — handles JS-rendered pages."""
    chunks = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            soup = BeautifulSoup(page.content(), "lxml")
            for el in soup.select(css_selector):
                chunks.append(el.get_text(separator=" ", strip=True))
        except PlaywrightTimeoutError:
            log.warning("Playwright timed out: %s", url)
        except Exception as exc:
            log.error("Playwright error on %s: %s", url, exc)
        finally:
            browser.close()
    return " | ".join(chunks[:60])


def scrape_reddit_json(url: str) -> str:
    headers = {"User-Agent": "FFCodeBot/2.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    posts = resp.json().get("data", {}).get("children", [])
    return " | ".join(
        f"{p['data'].get('title','')} {p['data'].get('selftext','')}"
        for p in posts[:20]
    )


def fetch_source_text(label: str, url: str, selector) -> str:
    log.info("Fetching: %s", label)
    try:
        return scrape_reddit_json(url) if selector is None else scrape_with_playwright(url, selector)
    except requests.RequestException as exc:
        log.error("Network error [%s]: %s", label, exc)
    except Exception as exc:
        log.error("Scrape error [%s]: %s", label, exc)
    return ""


# ─── Groq AI Extraction ──────────────────────────────────────────

def extract_code_with_groq(client: Groq, raw_text: str) -> dict:
    if not raw_text.strip():
        return {"has_code": False, "code": "", "reward": ""}

    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": GROQ_PROMPT.format(text=raw_text[:3000])}],
            temperature=0,
            max_tokens=150,
        )
        raw = completion.choices[0].message.content.strip()
        raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(raw)

        code = result.get("code", "").strip().upper()
        if result.get("has_code") and re.fullmatch(r"[A-Z0-9]{12}|[A-Z0-9]{16}", code):
            result["code"] = code
            return result

        if result.get("has_code"):
            log.warning("Groq found code but failed format check: '%s'", code)

    except json.JSONDecodeError as exc:
        log.error("Groq JSON parse error: %s", exc)
    except Exception as exc:
        log.error("Groq API error: %s", exc)

    return {"has_code": False, "code": "", "reward": ""}


# ─── Firebase Cloud Messaging ────────────────────────────────────

def send_fcm_notification(code: str, reward: str) -> bool:
    """
    Broadcast a high-priority FCM notification to the 'ff_codes' topic.
    All subscribed Android devices will receive it instantly.
    """
    reward_display = reward if reward else "Free Fire Reward"

    message = messaging.Message(
        topic=FCM_TOPIC,

        # Shown on the lock screen / notification tray
        notification=messaging.Notification(
            title="🔥 NEW FREE FIRE CODE DETECTED!",
            body=f"🎁 {reward_display}  |  🔑 {code}  — Tap to redeem instantly!",
        ),

        # Silent data payload — the Flutter app reads this
        data={
            "code":   code,
            "reward": reward_display,
            "url":    REDEEM_URL,
            "type":   "new_ff_code",
        },

        # Android-specific: maximum priority + sound + channel
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="ff_codes_channel",   # must match the Flutter channel id
                sound="default",
                priority="max",
                visibility="public",
                notification_count=1,
            ),
        ),
    )

    try:
        response = messaging.send(message)
        log.info("✅ FCM notification sent | Message ID: %s | Code: %s", response, code)
        return True
    except firebase_admin.exceptions.FirebaseError as exc:
        log.error("FCM send failed: %s", exc)
    except Exception as exc:
        log.error("Unexpected FCM error: %s", exc)

    return False


# ─── Main Check Cycle ────────────────────────────────────────────

def run_check_cycle(conn: sqlite3.Connection, groq_client: Groq) -> None:
    log.info("━━━ Starting check cycle ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    for label, url, selector in SOURCES:
        raw_text = fetch_source_text(label, url, selector)

        if not raw_text:
            log.info("No text from [%s], skipping.", label)
            continue

        result = extract_code_with_groq(groq_client, raw_text)

        if not result.get("has_code"):
            log.info("No code in [%s].", label)
            continue

        code   = result["code"]
        reward = result.get("reward", "")
        log.info("🔑 Code found in [%s]: %s | Reward: %s", label, code, reward or "N/A")

        if is_already_sent(conn, code):
            log.info("Code %s already sent — skipping duplicate.", code)
            continue

        # New code — push FCM notification
        sent = send_fcm_notification(code, reward)
        if sent:
            mark_as_sent(conn, code, reward, label)

    log.info("━━━ Cycle complete ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def validate_config() -> bool:
    problems = []
    if "YOUR_GROQ" in GROQ_API_KEY:
        problems.append("GROQ_API_KEY not configured")
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        problems.append(f"'{SERVICE_ACCOUNT_PATH}' file missing")
    for p in problems:
        log.error("CONFIG ERROR: %s", p)
    return len(problems) == 0


# ─── Entry Point ─────────────────────────────────────────────────

def main() -> None:
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║  FF Redeem Code Notifier v2 — Firebase FCM Mode  ║")
    log.info("╚══════════════════════════════════════════════════╝")

    if not validate_config():
        log.error("Fix the configuration errors above, then restart.")
        return

    init_firebase()
    conn        = init_db()
    groq_client = Groq(api_key=GROQ_API_KEY)

    log.info("Monitoring %d source(s) every %ds.", len(SOURCES), CHECK_INTERVAL_SECONDS)

    while True:
        try:
            run_check_cycle(conn, groq_client)
        except KeyboardInterrupt:
            log.info("Shutdown requested — exiting cleanly.")
            break
        except Exception as exc:
            log.exception("Unhandled exception in main loop: %s", exc)

        log.info("Sleeping %ds until next cycle…", CHECK_INTERVAL_SECONDS)
        time.sleep(CHECK_INTERVAL_SECONDS)

    conn.close()
    log.info("Goodbye!")


if __name__ == "__main__":
    main()
