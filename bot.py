import requests
import os
import html
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# ─── Optional: Google Sheets ──────────────────────────────────────────────────
try:
    import gspread
    from google.oauth2.service_account import Credentials
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
RAPIDAPI_KEY       = os.environ["RAPIDAPI_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GSHEET_CREDENTIALS = os.environ.get("GSHEET_CREDENTIALS", "")   # JSON string
GSHEET_ID          = os.environ.get("GSHEET_ID", "")
GSHEET_SHEET_NAME  = "Jobs"

SEEN_JOBS_FILE    = Path("seen_jobs.txt")
MAX_SEEN_JOBS     = 2000   # حداکثر تعداد ID ذخیره شده (جلوگیری از بزرگ شدن فایل)
MAX_JOBS_PER_RUN  = 15     # حداکثر آگهی ارسالی در هر اجرا

# ─── کلمات جستجو ──────────────────────────────────────────────────────────────
# جدا شده برای Remote و Relocation
REMOTE_QUERIES = [
    "Senior Software Engineer remote",
    "Senior Full Stack Developer remote",
    ".NET C# Angular remote",
    "Senior Backend Engineer remote",
    "Software Architect remote",
]

RELOCATION_QUERIES = [
    "Senior Software Engineer visa sponsorship relocation",
    "Senior Full Stack Developer relocation package",
    ".NET C# Angular relocation Germany",
    "Senior Software Engineer sponsorship Netherlands",
    "Software Engineer visa sponsorship Europe",
    "Senior Backend Engineer relocation Germany",
]

ALL_QUERIES = REMOTE_QUERIES + RELOCATION_QUERIES

# ─── کلمات کلیدی مثبت برای تشخیص نوع موقعیت ──────────────────────────────────────
RELOCATION_KEYWORDS = [
    "visa sponsorship", "sponsorship", "work visa", "relocation package",
    "relocation assistance", "relocation support", "visa assistance",
    "will sponsor", "sponsor visa", "global relocation", "international relocation"
]

REMOTE_KEYWORDS = [
    "remote", "work from home", "wfh", "fully remote", "100% remote",
    "anywhere", "distributed team", "home based"
]

# ─── کلمات ممنوعه (Blacklist) ──────────────────────────────────────────────────
BLACKLIST_KEYWORDS = [
    "us residents only", "must reside in us", "must be located in the us",
    "us citizens only", "green card required", "security clearance",
    "director", "agency", "recruitment agency", "staffing agency",
    "entry level", "junior", "internship", "fresher",
    "india only", "pakistan only", "philippines only"
]

# ─── کشورهای هدف برای Relocation ──────────────────────────────────────────────
TARGET_COUNTRIES = [
    "germany", "netherlands", "united kingdom", "uk", "sweden", "denmark",
    "norway", "finland", "switzerland", "austria", "ireland", "belgium",
    "france", "spain", "portugal", "italy", "canada", "australia"
]

# ══════════════════════════════════════════════════════════════════════════════
# حافظه دائمی — seen_jobs.txt
# ══════════════════════════════════════════════════════════════════════════════

def load_seen_jobs() -> set:
    """بارگذاری ID های قبلاً ارسال‌شده از فایل کش"""
    if SEEN_JOBS_FILE.exists():
        ids = set(line.strip() for line in SEEN_JOBS_FILE.read_text().splitlines() if line.strip())
        log.info(f"Loaded {len(ids)} seen job IDs from cache")
        return ids
    log.info("No cache file found — starting fresh")
    return set()


def save_seen_jobs(seen: set) -> None:
    """ذخیره ID ها — با محدودیت MAX_SEEN_JOBS برای جلوگیری از بزرگ شدن فایل"""
    ids_list = list(seen)
    if len(ids_list) > MAX_SEEN_JOBS:
        ids_list = ids_list[-MAX_SEEN_JOBS:]   # فقط جدیدترین‌ها نگه داشته میشه
    SEEN_JOBS_FILE.write_text("\n".join(ids_list))
    log.info(f"Saved {len(ids_list)} job IDs to cache")


# ══════════════════════════════════════════════════════════════════════════════
# JSearch API
# ══════════════════════════════════════════════════════════════════════════════

def search_jobs(query: str, retries: int = 3) -> list:
    """جستجو با retry خودکار و مدیریت rate limit"""
    url = "https://jsearch.p.rapidapi.com/search"
    headers = {
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "x-rapidapi-host": "jsearch.p.rapidapi.com",
    }
    params = {
        "query":          query,
        "num_pages":      "1",
        "date_posted":    "7days",  # افزایش به ۷ روز برای نتایج بیشتر
    }
    
    # اضافه کردن فیلتر remote فقط برای جستجوهای remote
    if query in REMOTE_QUERIES:
        params["work_from_home"] = "true"

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)

            if resp.status_code == 429:
                log.warning("Rate limit hit — waiting 60s before retry...")
                time.sleep(60)
                continue

            if resp.status_code == 403:
                log.error("API key invalid or not subscribed (403)")
                return []

            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "OK":
                log.warning(f"API non-OK for '{query}': {data.get('error')}")
                return []

            return data.get("data", [])

        except requests.exceptions.Timeout:
            log.warning(f"Timeout on attempt {attempt}/{retries} for '{query}'")
        except requests.exceptions.JSONDecodeError:
            log.error(f"Invalid JSON response for '{query}'")
            return []
        except requests.exceptions.RequestException as e:
            log.error(f"Request error (attempt {attempt}/{retries}): {e}")

        if attempt < retries:
            wait = 5 * attempt
            log.info(f"Waiting {wait}s before retry...")
            time.sleep(wait)

    log.error(f"All {retries} attempts failed for '{query}'")
    return []


# ══════════════════════════════════════════════════════════════════════════════
# تشخیص نوع موقعیت (Remote / Relocation)
# ══════════════════════════════════════════════════════════════════════════════

def detect_job_type(job: dict) -> dict:
    """
    تشخیص نوع موقعیت شغلی
    Returns: {'is_remote': bool, 'is_relocation': bool, 'visa_info': str, 'country': str}
    """
    title = (job.get("job_title") or "").lower()
    description = (job.get("job_description") or "").lower()
    combined = f"{title} {description}"
    
    result = {
        'is_remote': False,
        'is_relocation': False,
        'visa_info': "",
        'country': "",
        'job_type_label': "🏠 Remote"  # مقدار پیش‌فرض
    }
    
    # تشخیص Remote
    for keyword in REMOTE_KEYWORDS:
        if keyword in combined:
            result['is_remote'] = True
            result['job_type_label'] = "🏠 Remote"
            break
    
    # تشخیص Relocation/Visa Sponsorship
    for keyword in RELOCATION_KEYWORDS:
        if keyword in combined:
            result['is_relocation'] = True
            result['visa_info'] = keyword
            result['job_type_label'] = "✈️ Relocation + Visa"
            break
    
    # تشخیص کشور هدف
    job_country = (job.get("job_country") or "").lower()
    job_city = (job.get("job_city") or "").lower()
    
    for country in TARGET_COUNTRIES:
        if country in job_country or country in job_city:
            result['country'] = country.title()
            if result['is_relocation']:
                result['job_type_label'] = f"✈️ Relocation → {country.title()}"
            break
    
    # اگر هم remote هست هم relocation
    if result['is_remote'] and result['is_relocation']:
        result['job_type_label'] = "🌍 Remote + Relocation/Visa"
    
    return result


# ══════════════════════════════════════════════════════════════════════════════
# فیلتر Blacklist و فیلترهای اضافی بر اساس رزومه
# ══════════════════════════════════════════════════════════════════════════════

def is_blacklisted(job: dict) -> tuple:
    """
    بررسی Blacklist و فیلترهای اضافی
    Returns: (is_blocked: bool, reason: str)
    """
    title = (job.get("job_title") or "").lower()
    description = (job.get("job_description") or "").lower()
    country = (job.get("job_country") or "").lower()
    combined = f"{title} {description}"
    
    # Blacklist کلمات ممنوعه
    for keyword in BLACKLIST_KEYWORDS:
        if keyword.lower() in combined or keyword.lower() in country:
            log.info(f"  ⛔ Blacklisted '{job.get('job_title')}' — matched: '{keyword}'")
            return True, keyword
    
    # فیلتر Senior بودن (با توجه به رزومه شما)
    senior_required = ["senior", "lead", "architect", "staff engineer"]
    has_senior = any(word in title for word in senior_required)
    
    # اگر عنوان شامل Senior نبود ولی سطح پایین بود، فیلتر شود
    junior_words = ["junior", "entry", "trainee", "intern"]
    if any(word in title for word in junior_words):
        return True, "junior/entry level (not matching resume)"
    
    # Senior بودن الزامی نیست ولی اگر خیلی پایین بود فیلتر شود
    very_low = ["associate", "jr", "jr."]
    if any(word in title for word in very_low):
        return True, "associate/junior level"
    
    return False, ""


# ══════════════════════════════════════════════════════════════════════════════
# Telegram
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            log.error(f"Telegram error {resp.status_code}: {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram send exception: {e}")
        return False


def extract_salary(job: dict) -> str:
    """استخراج حقوق از فیلدهای مختلف API"""
    if job.get("job_salary_string"):
        return job["job_salary_string"]

    min_s  = job.get("job_min_salary")
    max_s  = job.get("job_max_salary")
    period = (job.get("job_salary_period") or "").lower()

    period_map = {"year": "/yr", "month": "/mo", "hour": "/hr", "week": "/wk"}
    period_label = period_map.get(period, f"/{period}" if period else "")

    if min_s and max_s:
        return f"${int(min_s):,} – ${int(max_s):,}{period_label}"
    if min_s:
        return f"${int(min_s):,}+{period_label}"
    return ""


def format_job(job: dict, job_type_info: dict) -> str:
    """ساخت متن پیام تلگرام با html.escape روی تمام متن‌ها"""
    title    = html.escape(job.get("job_title")    or "بدون عنوان")
    company  = html.escape(job.get("employer_name") or "نامشخص")
    city     = html.escape(job.get("job_city")     or "")
    country  = html.escape(job.get("job_country")  or "")
    location = f"{city}, {country}".strip(", ") or "Remote"
    source   = html.escape(job.get("job_publisher") or "")
    link     = job.get("job_apply_link") or job.get("job_google_link") or ""
    salary   = extract_salary(job)
    
    # ایموجی نوع موقعیت
    job_type_emoji = job_type_info['job_type_label']
    
    # اضافه کردن اطلاعات Visa اگر وجود دارد
    visa_note = ""
    if job_type_info['is_relocation'] and job_type_info['visa_info']:
        visa_note = f"\n🛂 <b>Visa/Relocation:</b> {html.escape(job_type_info['visa_info'].title())}"

    lines = [
        f"{job_type_emoji} <b>{title}</b>",
        f"🏢 {company}",
        f"📍 {location}",
    ]
    
    if visa_note:
        lines.append(visa_note)

    if salary:
        lines.append(f"💰 <b>{html.escape(salary)}</b>")

    if source:
        lines.append(f"🌐 {source}")

    if link:
        lines.append(f'🔗 <a href="{link}">Apply Now</a>')
    
    # اضافه کردن reminder برای تنظیم CV
    lines.append("\n<code>📝 Don't forget to customize CV for this position!</code>")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Google Sheets (اختیاری)
# ══════════════════════════════════════════════════════════════════════════════

def get_sheets_client():
    if not SHEETS_AVAILABLE:
        log.info("gspread not installed — skipping Google Sheets")
        return None
    if not GSHEET_CREDENTIALS or not GSHEET_ID:
        log.info("GSHEET_CREDENTIALS or GSHEET_ID not set — skipping Google Sheets")
        return None
    try:
        creds_dict = json.loads(GSHEET_CREDENTIALS)
        scopes     = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        log.info("Google Sheets connected ✅")
        return client
    except json.JSONDecodeError:
        log.error("GSHEET_CREDENTIALS is not valid JSON")
    except Exception as e:
        log.error(f"Google Sheets auth error: {e}")
    return None


def ensure_sheet_headers(client) -> None:
    if client is None:
        return
    try:
        sheet = client.open_by_key(GSHEET_ID).worksheet(GSHEET_SHEET_NAME)
        first_row = sheet.row_values(1)
        if not first_row:
            headers = ["Job Title", "Company", "Apply Link", "Posted Date",
                       "City", "Country", "Salary", "Job Type", "Visa Info", "Saved At (UTC)"]
            sheet.insert_row(headers, 1)
            log.info("Sheet headers created")
    except Exception as e:
        log.error(f"Sheet header check error: {e}")


def append_to_sheet(client, job: dict, job_type_info: dict) -> None:
    if client is None:
        return
    try:
        sheet = client.open_by_key(GSHEET_ID).worksheet(GSHEET_SHEET_NAME)
        posted = (job.get("job_posted_at_datetime_utc") or "")[:10]
        row = [
            job.get("job_title", ""),
            job.get("employer_name", ""),
            job.get("job_apply_link") or job.get("job_google_link") or "",
            posted,
            job.get("job_city", ""),
            job.get("job_country", ""),
            extract_salary(job),
            job_type_info['job_type_label'],
            job_type_info.get('visa_info', ''),
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.error(f"Sheet append error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info(f"═══ Bot started at {now} ═══")

    seen_jobs     = load_seen_jobs()
    sheets_client = get_sheets_client()
    ensure_sheet_headers(sheets_client)

    new_jobs      = []
    blacklisted   = 0
    already_seen  = 0
    errors        = 0
    remote_count  = 0
    relocation_count = 0

    for query in ALL_QUERIES:
        log.info(f"Searching: '{query}'")
        try:
            jobs = search_jobs(query)
            log.info(f"  → {len(jobs)} raw results")

            for job in jobs:
                try:
                    job_id = job.get("job_id") or job.get("job_apply_link") or ""
                    if not job_id:
                        continue

                    if job_id in seen_jobs:
                        already_seen += 1
                        continue

                    seen_jobs.add(job_id)

                    # بررسی Blacklist
                    is_blocked, block_reason = is_blacklisted(job)
                    if is_blocked:
                        blacklisted += 1
                        log.info(f"  ⛔ Blocked: {job.get('job_title')} - {block_reason}")
                        continue

                    # تشخیص نوع موقعیت
                    job_type_info = detect_job_type(job)
                    
                    # آمار
                    if job_type_info['is_remote']:
                        remote_count += 1
                    if job_type_info['is_relocation']:
                        relocation_count += 1
                    
                    # ذخیره اطلاعات نوع موقعیت همراه با job
                    new_jobs.append((job, job_type_info))

                except Exception as e:
                    log.error(f"  Error processing job item: {e}")
                    errors += 1
                    continue

        except Exception as e:
            log.error(f"Error in query '{query}': {e}")
            errors += 1
            continue

        time.sleep(1.5)

    # حذف تکراری‌ها
    dedup_seen = set()
    unique_jobs = []
    for job, job_type in new_jobs:
        jid = job.get("job_id", "")
        if jid and jid not in dedup_seen:
            dedup_seen.add(jid)
            unique_jobs.append((job, job_type))

    log.info(f"Summary → new: {len(unique_jobs)} | remote: {remote_count} | relocation: {relocation_count} | blacklisted: {blacklisted} | already seen: {already_seen} | errors: {errors}")

    # ─── ارسال به تلگرام ───────────────────────────────────────────────────
    if not unique_jobs:
        send_telegram(
            f"🔍 <b>📋 گزارش روزانه - جستجوی کار</b>\n"
            f"📅 {now}\n\n"
            f"✅ آگهی جدیدی امروز پیدا نشد.\n"
            f"🏠 Remote found: {remote_count} | ✈️ Relocation: {relocation_count}\n"
            f"⛔ فیلتر شده: {blacklisted} | 🔁 تکراری: {already_seen}\n\n"
            f"<i>🔁 دوباره امتحان میکنم...</i>"
        )
        save_seen_jobs(seen_jobs)
        return

    # پیام هدر با آمار
    header_msg = (
        f"🔍 <b>🎯 آگهی‌های شغلی جدید - مطابق با رزومه</b>\n"
        f"📅 {now}\n"
        f"📊 {len(unique_jobs)} آگهی جدید\n"
        f"🏠 Remote: {remote_count} | ✈️ Relocation/Visa: {relocation_count}\n"
        f"⛔ فیلتر شده: {blacklisted}\n"
        f"➖➖➖➖➖➖➖➖"
    )
    send_telegram(header_msg)
    time.sleep(1)

    sent = 0
    for job, job_type in unique_jobs[:MAX_JOBS_PER_RUN]:
        try:
            msg = format_job(job, job_type)
            if send_telegram(msg):
                sent += 1
                append_to_sheet(sheets_client, job, job_type)
            time.sleep(0.8)
        except Exception as e:
            log.error(f"Error sending job to Telegram: {e}")
            continue

    save_seen_jobs(seen_jobs)
    log.info(f"═══ Done. Sent {sent}/{len(unique_jobs)} jobs ═══")


if __name__ == "__main__":
    main()
