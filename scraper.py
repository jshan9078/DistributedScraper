import os
import re
import time
import random
import json
import psycopg2
import requests
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from google.cloud import storage

# =============================
# CONFIGURATION
# =============================
MAX_IMAGES = 10000
LOG_FILE = "scrape_log.json"

WAIT_MIN, WAIT_MAX = 20, 30
BREAK_EVERY_N_REQUESTS = 50
BREAK_DURATION = 60
TIMEOUT = 15
RATE_LIMIT_COOLDOWN = 600
CONSECUTIVE_ERRORS_THRESHOLD = 3
JUMP_MIN, JUMP_MAX = 100, 500

# Exploration bounds
CERT_MIN = 100000001
CERT_MAX = 123371178

# Recheck cadence after exploration
FALLBACK_RECHECK_INTERVAL = 10
FALLBACK_RECHECK_SLEEP = 30

# --- Environment variables (Kubernetes) ---
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER", "psa")
DB_NAME = os.getenv("DB_NAME", "psa")
GCS_BUCKET = os.getenv("GCS_BUCKET")
WORKER_ID = os.getenv("HOSTNAME", "unknown")

# --- GCS setup ---
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET)

# =============================
# DATABASE CONNECTION
# =============================
def get_db_conn():
    """Connect to PostgreSQL using environment variables and print debug info."""
    db_host = os.getenv("DB_HOST")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD", "").strip()
    db_name = os.getenv("DB_NAME")

    print("========== DATABASE CONNECTION DEBUG ==========")
    print(f"DB_HOST: {db_host}")
    print(f"DB_USER: {db_user}")
    print(f"DB_NAME: {db_name}")
    print(f"DB_PASSWORD length: {len(db_password)}")
    print("===============================================")

    safe_password = quote_plus(db_password)
    conn_str = f"postgresql://{db_user}:{safe_password}@{db_host}/{db_name}"
    print("Connecting using connection string (password hidden)...")

    try:
        conn = psycopg2.connect(conn_str)
        print("✅ Successfully connected to database.")
        return conn
    except psycopg2.OperationalError as e:
        print("❌ Failed to connect to database.")
        print("Error:", e)
        raise
    except Exception as e:
        print("⚠️ Unexpected error while connecting to database.")
        print("Error:", e)
        raise

def fetch_next_cert(conn):
    """Atomically claim one pending cert_id (no race between SELECT/UPDATE)."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH next_job AS (
                SELECT cert_id
                FROM work_queue
                WHERE status = 'pending'
                ORDER BY cert_id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE work_queue
            SET status = 'in_progress',
                worker_id = %s,
                updated_at = NOW()
            FROM next_job
            WHERE work_queue.cert_id = next_job.cert_id
                AND work_queue.status = 'pending'
            RETURNING work_queue.cert_id;
        """, (WORKER_ID,))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None

def mark_cert_complete(conn, cert_id):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE work_queue
            SET status = 'done', updated_at = NOW()
            WHERE cert_id = %s
        """, (cert_id,))
        conn.commit()

def insert_new_cert(conn, cert_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO work_queue (cert_id, status)
            VALUES (%s, 'pending')
            ON CONFLICT (cert_id) DO NOTHING
        """, (cert_id,))
        conn.commit()

def check_queue_nonempty(conn):
    """Lightweight check: returns True if pending work exists."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM work_queue WHERE status='pending' LIMIT 1")
        return cur.fetchone() is not None

# =============================
# SELENIUM SETUP
# =============================
def setup_driver(headless=True):
    print("🧠 [DRIVER] Initializing Chrome driver...")
    options = Options()
    if headless:
        print("🧠 [DRIVER] Running in headless mode.")
        # modern headless
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    # Stealth-ish / stability flags
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/123.0.0.0 Safari/537.36")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--remote-debugging-port=9222")

    # clear old sockets just in case
    os.system("rm -rf /tmp/.com.google.Chrome* || true")

    try:
        print("🧠 [DRIVER] Launching Chromium...")
        service = Service("/usr/bin/chromedriver")
        driver = webdriver.Chrome(service=service, options=options)
        print("✅ [DRIVER] Chrome started successfully.")
        return driver
    except Exception as e:
        print(f"❌ [DRIVER] Failed to start Chrome: {e}")
        raise

def fetch_page_selenium(driver, cert_id):
    url = f"https://www.psacard.com/cert/{cert_id}/psa"
    print(f"🌐 [FETCH] Fetching {url}")
    try:
        from selenium.common.exceptions import StaleElementReferenceException

        driver.get(url)
        wait = WebDriverWait(driver, 20)

        # 1) Wait for the cert number element to appear
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "p.text-subtitle1")))

        # 2) Wait until at least one content image has real https src (avoid base64 placeholders)
        def check_images_loaded(d):
            try:
                imgs = d.find_elements(By.CSS_SELECTOR, "img[itemprop='contentUrl']")
                return any(
                    (s := img.get_attribute("src")) and s.startswith("http")
                    for img in imgs
                )
            except StaleElementReferenceException:
                # Elements changed, retry on next poll
                return False

        wait.until(check_images_loaded)
        print("🖼️ [FETCH] Found valid HTTPS image URLs.")

        # 3) Optional: wait until DOM stabilizes a bit
        last_html = ""
        stable_count = 0
        for _ in range(10):
            current_html = driver.page_source
            if current_html == last_html:
                stable_count += 1
                if stable_count >= 2:
                    break
            else:
                stable_count = 0
                last_html = current_html
            time.sleep(0.5)

        print(f"✅ [FETCH] Page {cert_id} loaded successfully.")
        return driver.page_source

    except Exception as e:
        print(f"❌ [FETCH] Error fetching {cert_id}: {e}")
        return None

# =============================
# UTILS
# =============================
def is_page_loaded(html, cert_id):
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("p", class_="text-subtitle1")
    return tag and tag.get_text(strip=True) == f"#{cert_id}"

def is_pokemon_page(html):
    text = html.lower()
    return "pokemon" in text, any(k in text for k in ["japanese", "asia", "chinese"])

def upgrade_to_large(url):
    if not url:
        return url
    large_url = url.replace("/small/", "/large/")
    try:
        if requests.head(large_url, timeout=5).ok:
            return large_url
    except Exception:
        pass
    return url

def parse_image_urls(html):
    soup = BeautifulSoup(html, "html.parser")
    imgs = soup.find_all("img", {"itemprop": "contentUrl"})
    if len(imgs) < 2:
        return None, None

    def valid_url(src):
        return src and src.startswith("http")

    front = imgs[0].get("src")
    back = imgs[1].get("src")

    if not valid_url(front):
        print(f"⚠️ [PARSE] Front image is placeholder ({str(front)[:30]}...), skipping.")
        front = None
    if not valid_url(back):
        print(f"⚠️ [PARSE] Back image is placeholder ({str(back)[:30]}...), skipping.")
        back = None

    return upgrade_to_large(front), upgrade_to_large(back)

# --- Reference resolution for PSA large images ---
REF_W, REF_H = 1024, 1768
BASE_FRONT = dict(left=110, top=467, right=920, bottom=1625)
BASE_BACK  = dict(left=110, top=467, right=915, bottom=1622)

def crop_card(img, side="front"):
    w, h = img.size
    base = BASE_FRONT if side == "front" else BASE_BACK
    left_n, top_n, right_n, bottom_n = base["left"]/REF_W, base["top"]/REF_H, base["right"]/REF_W, base["bottom"]/REF_H
    return img.crop((int(left_n*w), int(top_n*h), int(right_n*w), int(bottom_n*h)))

def upload_image_to_gcs(img, cert_id, side, grade="unknown"):
    blob_path = f"png/{grade}/{cert_id}_{side}.png"
    blob = bucket.blob(blob_path)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    blob.upload_from_file(buf, content_type="image/png")
    print(f"✅ Uploaded {blob_path}")

def extract_grade(html):
    """
    Extracts PSA grade (1–10) from either the text 'PSA 10'
    or from <dd> labels like 'NM-MT 8', 'GEM MT 10', etc.
    Returns 'unknown' if no valid grade found.
    """
    m = re.search(r"\bPSA\s*([0-9]{1,2})\b", html, re.IGNORECASE)
    if m and 1 <= int(m.group(1)) <= 10:
        return int(m.group(1))

    m = re.search(
        r"\b(?:PR|GOOD|VG|VG-EX|EX|EX-MT|NM|NM-MT|MINT|GEM\s*MT)\s*([0-9]{1,2})\b",
        html, re.IGNORECASE)
    if m and 1 <= int(m.group(1)) <= 10:
        return int(m.group(1))

    return "unknown"

# =============================
# MAIN SCRAPER (CHAIN)
# =============================
def process_chain(conn, start_cert_id, already_claimed=False):
    """Sequentially process cards starting from start_cert_id, avoiding overlap between pods."""
    count = 0
    chain_cert = start_cert_id
    consecutive_errors = 0
    is_claimed = already_claimed

    while True:
        # --- Guard: if this cert is NOT already claimed by fetch_next_cert(), check status ---
        if not is_claimed:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM work_queue WHERE cert_id = %s", (chain_cert,))
                row = cur.fetchone()

                # --- Skip only if another pod already owns or completed this cert ---
                if row and row[0] != "pending":
                    print(f"⚠️ [CHAIN] Cert {chain_cert} already {row[0]} — skipping.")
                    next_cert = fetch_next_cert(conn)
                    if not next_cert:
                        print("📭 [QUEUE] No available pending certs — exiting chain.")
                        break
                    chain_cert = next_cert
                    is_claimed = True  # fetch_next_cert already claimed it
                    continue

                # If no row, reserve as in_progress so others skip it
                if not row:
                    cur.execute("""
                        INSERT INTO work_queue (cert_id, status, worker_id, updated_at)
                        VALUES (%s, 'in_progress', %s, NOW())
                        ON CONFLICT (cert_id) DO NOTHING
                    """, (chain_cert, WORKER_ID))
                    conn.commit()

        # Reset the flag - we're now processing this cert
        is_claimed = False

        print(f"🔁 [CHAIN] Starting cert {chain_cert}")
        driver = setup_driver(headless=True)
        html = fetch_page_selenium(driver, chain_cert)
        print("🧹 [CHAIN] Closing Chrome driver.")
        driver.quit()

        if not html:
            consecutive_errors += 1
            print(f"⚠️ [CHAIN] Error fetching {chain_cert} ({consecutive_errors} consecutive).")
            if consecutive_errors >= CONSECUTIVE_ERRORS_THRESHOLD:
                print("🚫 [CHAIN] Too many consecutive errors — cooling down 10 min.")
                time.sleep(RATE_LIMIT_COOLDOWN)
                consecutive_errors = 0

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE work_queue
                    SET status = 'error', updated_at = NOW()
                    WHERE cert_id = %s
                """, (chain_cert,))
                conn.commit()

            # Try next from queue
            next_cert = fetch_next_cert(conn)
            if next_cert:
                print(f"📬 [QUEUE] Switching to next queued cert {next_cert}")
                chain_cert = next_cert
                is_claimed = True  # fetch_next_cert already claimed it
                continue
            else:
                print("📭 [QUEUE] Empty queue — stopping chain.")
                break

        consecutive_errors = 0

        # --- Page check logic ---
        if not is_page_loaded(html, chain_cert):
            print(f"⚠️ [CHAIN] Page {chain_cert} not loaded — marking stale and stopping chain.")
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE work_queue
                    SET status = 'stale', updated_at = NOW()
                    WHERE cert_id = %s
                """, (chain_cert,))
                conn.commit()
            break

        has_poke, is_jap = is_pokemon_page(html)
        if not has_poke or is_jap:
            reason = "Japanese/Asian" if is_jap else "Non-Pokémon"
            print(f"🇯🇵 [CHAIN] {reason} card {chain_cert} — marking skipped and ending chain.")
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE work_queue
                    SET status = 'skipped', updated_at = NOW()
                    WHERE cert_id = %s
                """, (chain_cert,))
                conn.commit()

            # Non-Pokemon breaks the chain, return to queue mode
            print("🔗 [CHAIN] Chain broken — returning to queue mode.")
            break

        # --- Image processing and upload ---
        print(f"🧠 [CHAIN] Parsing cert {chain_cert}...")
        front_url, back_url = parse_image_urls(html)
        if not front_url or not back_url:
            print(f"⚠️ [CHAIN] Missing images for {chain_cert}, marking skipped and ending chain.")
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE work_queue
                    SET status = 'skipped', updated_at = NOW()
                    WHERE cert_id = %s
                """, (chain_cert,))
            conn.commit()
            # Missing images breaks the chain, return to queue mode
            print("🔗 [CHAIN] Chain broken — returning to queue mode.")
            break

        grade = extract_grade(html)
        print(f"🃏 [CHAIN] Pokémon card {chain_cert} (grade={grade})")

        for side, url in [("front", front_url), ("back", back_url)]:
            try:
                print(f"⬇️ [DOWNLOAD] {side} image for {chain_cert}...")
                r = requests.get(url, timeout=TIMEOUT)
                img = Image.open(BytesIO(r.content)).convert("RGB")
                cropped = crop_card(img, side)
                upload_image_to_gcs(cropped, chain_cert, side, grade)
            except Exception as e:
                print(f"❌ [UPLOAD] {chain_cert} {side} failed: {e}")

        mark_cert_complete(conn, chain_cert)
        count += 1
        print(f"✅ [CHAIN] Finished {chain_cert}. Continuing chain to cert {chain_cert + 1}...")

        # Continue the chain: try cert_id + 1
        chain_cert += 1
        is_claimed = False  # Next cert in chain needs to be checked/claimed
        continue

    return count

# =============================
# ORCHESTRATOR (RUN LOOP)
# =============================
def run_scraper():
    conn = get_db_conn()
    last_cert_id = None
    fallback_counter = 0
    total_processed = 0

    print("=" * 70)
    print("🚀 PSA Scraper — CloudSQL + GCS Mode (with overlap prevention)")
    print("=" * 70)

    while total_processed < MAX_IMAGES:
        # 1) Prefer pending work from queue
        cert_id = fetch_next_cert(conn)
        if cert_id:
            mode = "QUEUE"
            already_claimed = True  # fetch_next_cert already claimed it
        else:
            # 2) Exploration fallback
            fallback_counter += 1
            mode = "EXPLORATION"
            already_claimed = False  # exploration cert not claimed yet

            # Start from last known position, but enforce minimum bound
            start_pos = max(last_cert_id or CERT_MIN, CERT_MIN)

            # Add worker-specific offset to reduce collision between pods
            # Hash worker ID to get a consistent but unique offset per pod
            worker_offset = hash(WORKER_ID) % 1000
            candidate = start_pos + random.randint(JUMP_MIN, JUMP_MAX) + worker_offset

            # bounds check
            if candidate > CERT_MAX:
                print(f"🔄 [RESET] Upper bound {CERT_MAX} reached. Resetting to {CERT_MIN}.")
                candidate = CERT_MIN
            elif candidate < CERT_MIN:
                print(f"🔄 [RESET] Below lower bound. Resetting to {CERT_MIN}.")
                candidate = CERT_MIN

            # skip if already processed/claimed
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM work_queue WHERE cert_id = %s", (candidate,))
                row = cur.fetchone()
            if row and row[0] in ('done', 'in_progress', 'error', 'stale', 'skipped'):
                print(f"⏭️ [EXPLORATION] Skipping cert {candidate} (already {row[0]}).")
                # jump forward and loop again
                last_cert_id = candidate
                continue

            # register candidate into queue (pending) so others see it
            print(f"📭 [EXPLORATION] Queue empty — exploring random chain at {candidate}")
            insert_new_cert(conn, candidate)
            cert_id = candidate

        print(f"🧾 [{mode}] Found cert {cert_id} — processing chain.")
        processed = process_chain(conn, cert_id, already_claimed=already_claimed)
        total_processed += processed
        last_cert_id = cert_id

        # After each exploration step, recheck if queue has pending
        if mode == "EXPLORATION":
            print("🔄 [CHECK] Rechecking queue after exploration...")
            if check_queue_nonempty(conn):
                print("✅ [QUEUE] New pending entries detected — switching back to queue mode.")

        # Respectful pacing
        time.sleep(random.uniform(WAIT_MIN, WAIT_MAX))

# =============================
# ENTRY POINT
# =============================
if __name__ == "__main__":
    run_scraper()
