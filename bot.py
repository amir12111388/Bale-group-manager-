import json
import logging
import os
import re
import signal
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv

from core.bale_api import BaleAPI, BaleAPIError, btn, inline_keyboard
from core.storage import Storage, jalali_date, now_ts

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/bale_group_manager.sqlite3")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
OWNER_IDS = [int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()]
DEFAULT_FLOOD_LIMIT = int(os.getenv("DEFAULT_FLOOD_LIMIT", "6"))
DEFAULT_FLOOD_WINDOW = int(os.getenv("DEFAULT_FLOOD_WINDOW", "8"))
PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED", "0") == "1"
BALE_PROVIDER_TOKEN = os.getenv("BALE_PROVIDER_TOKEN", "").strip()
CLEAR_PENDING_ON_START = os.getenv("CLEAR_PENDING_ON_START", "1") == "1"

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/bot.log", encoding="utf-8")],
)
log = logging.getLogger("bale_group_manager")

api = BaleAPI(BOT_TOKEN, logger=log)
db = Storage(DB_PATH)
db.add_owner_admins(OWNER_IDS)

RUNNING = True
admin_cache: Dict[int, Tuple[float, set[int]]] = {}
flood_cache: Dict[Tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=30))

LINK_RE = re.compile(r"(?i)(https?://|www\.|t\.me/|ble\.ir/|bale\.ai/|telegram\.me/|@\w{4,})")
MENTION_RE = re.compile(r"(?<!\w)@\w{4,}")
COMMAND_RE = re.compile(r"^/([^\s@]+)(?:@\w+)?(?:\s+(.*))?$", re.S)


def stop_handler(signum, frame):
    global RUNNING
    RUNNING = False
    log.warning("Shutdown signal received")


signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT, stop_handler)


def get_message(update: dict) -> Optional[dict]:
    return update.get("message") or update.get("edited_message")


def msg_chat(msg: dict) -> dict:
    return msg.get("chat") or {}


def msg_from(msg: dict) -> dict:
    return msg.get("from") or msg.get("sender") or {}


def chat_id(msg: dict) -> Optional[int | str]:
    return msg_chat(msg).get("id")


def chat_type(msg: dict) -> str:
    return msg_chat(msg).get("type") or ""


def message_id(msg: dict) -> Optional[int]:
    return msg.get("message_id") or msg.get("messageId")


def user_id(user: dict) -> Optional[int]:
    try:
        return int(user.get("id"))
    except Exception:
        return None


def is_group(msg: dict) -> bool:
    ctype = chat_type(msg).lower()
    return ctype in {"group", "supergroup", "channel"} or str(chat_id(msg)).startswith("-")


def is_private(msg: dict) -> bool:
    return chat_type(msg).lower() == "private" or not is_group(msg)


def first_name(user: dict) -> str:
    return user.get("first_name") or user.get("firstName") or "کاربر"


def username(user: dict) -> str:
    return user.get("username") or ""


def get_text(msg: dict) -> str:
    return msg.get("text") or msg.get("caption") or ""


def reply_text(chat, text, msg: Optional[dict] = None, **kwargs):
    reply_to = message_id(msg) if msg else None
    try:
        return api.send_message(chat, text, reply_to_message_id=reply_to, **kwargs)
    except Exception as exc:
        log.warning("send message failed: %s", exc)
        return None


PERSIAN_DIGIT_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fa_norm(text: str) -> str:
    """Normalize Persian/Arabic text for command parsing."""
    text = (text or "").translate(PERSIAN_DIGIT_TRANS)
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = text.replace("ة", "ه").replace("ۀ", "ه")
    text = text.replace("\u200c", " ").replace("ـ", "")
    text = re.sub(r"[!؛،,.?؟]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


SLASH_CMD_ALIASES = {
    "شروع": "start", "استارت": "start",
    "راهنما": "help", "کمک": "help",
    "پنل": "panel", "تنظیمات": "panel",
    "ایدی": "id", "آیدی": "id", "شناسه": "id",
    "قفل": "lock", "باز": "unlock", "بازکردن": "unlock", "باز کردن": "unlock", "رفعقفل": "unlock", "رفع قفل": "unlock",
    "بن": "ban", "مسدود": "ban", "مسدودکردن": "ban",
    "رفع بن": "unban", "ازاد": "unban", "آزاد": "unban", "ازادسازی": "unban", "آزادسازی": "unban",
    "کیک": "kick", "اخراج": "kick", "بیرون": "kick",
    "اخطار": "warn", "هشدار": "warn",
    "حذف اخطار": "unwarn", "پاک اخطار": "unwarn", "رفع اخطار": "unwarn",
    "اخطارها": "warnings", "اخطار ها": "warnings",
    "میوت": "mute", "سکوت": "mute", "خفه": "mute",
    "رفع میوت": "unmute", "حذف میوت": "unmute", "رفع سکوت": "unmute",
    "تنظیم خوشامد": "setwelcome", "خوشامد": "welcome", "تنظیم قوانین": "setrules", "قوانین": "rules",
    "جوین اجباری": "forcejoin", "عضویت اجباری": "forcejoin",
    "فیلتر": "filter", "فیلترها": "filters", "فیلتر ها": "filters",
    "نوت": "note", "یادداشت": "note", "نوتها": "notes", "نوت ها": "notes", "یادداشتها": "notes",
    "افزودن ادمین": "addadmin", "افزودن مدیر": "addadmin", "اضافه ادمین": "addadmin", "اضافه مدیر": "addadmin",
    "حذف ادمین": "deladmin", "حذف مدیر": "deladmin",
    "ادمینها": "admins", "ادمین ها": "admins", "مدیرها": "admins", "مدیران": "admins",
    "آمار": "stats", "امار": "stats", "بکاپ": "backup", "پشتیبان": "backup", "نسخه پشتیبان": "backup",
}

LOCK_TARGET_ALIASES = {
    "لینک": "لینک", "لینکها": "لینک", "لینک ها": "لینک",
    "منشن": "منشن", "تگ": "منشن", "یوزرنیم": "منشن",
    "مدیا": "مدیا", "رسانه": "مدیا", "عکس": "مدیا", "فیلم": "مدیا", "ویدیو": "مدیا", "فایل": "مدیا", "صدا": "مدیا", "ویس": "مدیا",
    "استیکر": "استیکر", "استیکرها": "استیکر", "استیکر ها": "استیکر",
    "فوروارد": "فوروارد", "فروارد": "فوروارد", "پیام فورواردی": "فوروارد",
    "فلود": "فلود", "اسپم": "فلود", "ضد فلود": "فلود",
    "سرویس": "سرویس", "پیام سرویس": "سرویس", "ورود خروج": "سرویس", "ورود و خروج": "سرویس",
    "همه": "همه", "همه قفلها": "همه", "همه قفل ها": "همه",
}


def normalize_digits(text: str) -> str:
    return (text or "").translate(PERSIAN_DIGIT_TRANS)


def _starts(text: str, phrases: list[str]) -> Optional[Tuple[str, str]]:
    for phrase in sorted([fa_norm(x) for x in phrases], key=len, reverse=True):
        if text == phrase:
            return phrase, ""
        if text.startswith(phrase + " "):
            return phrase, text[len(phrase):].strip()
    return None


def _normalize_lock_target(text: str) -> str:
    t = fa_norm(text)
    t = re.sub(r"^(را|رو|را هم|رو هم)\s+", "", t).strip()
    return LOCK_TARGET_ALIASES.get(t, t)


def _parse_persian_plain_command(text: str) -> Optional[Tuple[str, str]]:
    t = fa_norm(text)
    if not t:
        return None

    exact = {
        "راهنما": ("help", ""), "کمک": ("help", ""),
        "پنل": ("panel", ""), "تنظیمات": ("panel", ""),
        "آیدی": ("id", ""), "ایدی": ("id", ""), "شناسه": ("id", ""),
        "قوانین": ("rules", ""),
        "فیلترها": ("filters", ""), "فیلتر ها": ("filters", ""),
        "نوتها": ("notes", ""), "نوت ها": ("notes", ""), "یادداشتها": ("notes", ""),
        "آمار": ("stats", ""), "امار": ("stats", ""),
        "بکاپ": ("backup", ""), "نسخه پشتیبان": ("backup", ""),
        "ادمینها": ("admins", ""), "ادمین ها": ("admins", ""), "مدیران": ("admins", ""),
    }
    if t in exact:
        return exact[t]

    m = _starts(t, ["قفل کردن", "قفل کن", "قفل"])
    if m:
        return "lock", _normalize_lock_target(m[1])
    m = _starts(t, ["باز کردن", "بازکردن", "باز کن", "بازکن", "رفع قفل", "باز"])
    if m:
        return "unlock", _normalize_lock_target(m[1])
    for alias in sorted(LOCK_TARGET_ALIASES, key=len, reverse=True):
        target = LOCK_TARGET_ALIASES[alias]
        if re.fullmatch(rf"{re.escape(alias)}\s+(را|رو\s+)?قفل\s*(کن|شه|شود)?", t):
            return "lock", target
        if re.fullmatch(rf"{re.escape(alias)}\s+(را|رو\s+)?(باز|آزاد|ازاد)\s*(کن|شه|شود)?", t):
            return "unlock", target

    m = _starts(t, ["تنظیم خوشامد", "متن خوشامد", "خوشامد جدید"])
    if m:
        return "setwelcome", m[1]
    m = _starts(t, ["تنظیم قوانین", "متن قوانین", "قوانین جدید"])
    if m:
        return "setrules", m[1]
    if t in {"خوشامد روشن", "خوشامد فعال", "روشن کردن خوشامد", "فعال کردن خوشامد"}:
        return "welcome", "on"
    if t in {"خوشامد خاموش", "خوشامد غیرفعال", "خاموش کردن خوشامد", "غیرفعال کردن خوشامد"}:
        return "welcome", "off"

    prefix_map = [
        (["رفع بن", "حذف بن", "آزادسازی", "ازادسازی", "آزاد", "ازاد"], "unban"),
        (["بن کردن", "بن کن", "بن", "مسدود کردن", "مسدود کن", "مسدود"], "ban"),
        (["بیرون کردن", "بیرون کن", "اخراج کردن", "اخراج کن", "اخراج", "کیک"], "kick"),
        (["حذف اخطار", "پاک کردن اخطار", "پاک اخطار", "رفع اخطار"], "unwarn"),
        (["نمایش اخطار", "اخطارها", "اخطار ها"], "warnings"),
        (["اخطار دادن", "اخطار بده", "اخطار", "هشدار دادن", "هشدار بده", "هشدار"], "warn"),
        (["رفع میوت", "حذف میوت", "رفع سکوت", "حذف سکوت"], "unmute"),
        (["میوت کردن", "میوت کن", "میوت", "سکوت کردن", "سکوت کن", "سکوت", "خفه کردن", "خفه کن"], "mute"),
    ]
    for phrases, cmd in prefix_map:
        m = _starts(t, phrases)
        if m:
            return cmd, m[1]

    m = _starts(t, ["جوین اجباری", "عضویت اجباری"])
    if m:
        rest = m[1]
        a = _starts(rest, ["افزودن", "اضافه", "اضافه کردن", "افزودن کانال", "اضافه کانال"])
        if a:
            return "forcejoin", "add " + a[1]
        a = _starts(rest, ["حذف", "پاک", "برداشتن", "حذف کانال"])
        if a:
            return "forcejoin", "del " + a[1]
        if rest in {"لیست", "نمایش", "کانالها", "کانال ها"} or not rest:
            return "forcejoin", "list"
        return "forcejoin", rest

    m = _starts(t, ["افزودن فیلتر", "اضافه فیلتر", "فیلتر افزودن", "فیلتر اضافه"])
    if m:
        return "filter", "add " + m[1]
    m = _starts(t, ["حذف فیلتر", "پاک فیلتر", "فیلتر حذف", "فیلتر پاک"])
    if m:
        return "filter", "del " + m[1]
    m = _starts(t, ["لیست فیلتر", "نمایش فیلتر"])
    if m:
        return "filters", ""
    m = _starts(t, ["افزودن نوت", "اضافه نوت", "نوت افزودن", "نوت اضافه", "افزودن یادداشت", "اضافه یادداشت"])
    if m:
        return "note", "add " + m[1]
    m = _starts(t, ["حذف نوت", "پاک نوت", "نوت حذف", "نوت پاک", "حذف یادداشت", "پاک یادداشت"])
    if m:
        return "note", "del " + m[1]
    m = _starts(t, ["نوت", "یادداشت"])
    if m:
        return "note", m[1]

    m = _starts(t, ["افزودن ادمین", "اضافه ادمین", "افزودن مدیر", "اضافه مدیر", "ادمین کردن"])
    if m:
        return "addadmin", m[1]
    m = _starts(t, ["حذف ادمین", "پاک ادمین", "حذف مدیر", "پاک مدیر"])
    if m:
        return "deladmin", m[1]

    return None


def get_command(text: str) -> Optional[Tuple[str, str]]:
    raw = (text or "").strip()
    m = COMMAND_RE.match(raw)
    if m:
        raw_cmd = fa_norm(m.group(1))
        args = normalize_digits((m.group(2) or "").strip())
        cmd = SLASH_CMD_ALIASES.get(raw_cmd, raw_cmd.lower())
        return cmd, args
    return _parse_persian_plain_command(raw)


def has_media(msg: dict) -> bool:
    media_fields = ("photo", "video", "document", "audio", "animation", "voice", "video_note")
    return any(k in msg for k in media_fields)


def has_sticker(msg: dict) -> bool:
    return "sticker" in msg


def is_forwarded(msg: dict) -> bool:
    return bool(msg.get("forward_from") or msg.get("forward_from_chat") or msg.get("forward_date"))


def get_forwarded_user_id(msg: dict) -> Optional[int]:
    f = msg.get("forward_from") or {}
    try:
        return int(f.get("id")) if f.get("id") else None
    except Exception:
        return None


def get_target_from_reply_or_arg(msg: dict, args: str) -> Tuple[Optional[int], str]:
    reply = msg.get("reply_to_message") or {}
    if reply.get("from"):
        uid = user_id(reply.get("from") or {})
        if uid:
            return uid, args.strip()
    parts = args.split(maxsplit=1)
    if parts and parts[0].lstrip("-").isdigit():
        return int(parts[0]), parts[1].strip() if len(parts) > 1 else ""
    fuid = get_forwarded_user_id(msg)
    if fuid:
        return fuid, args.strip()
    return None, args.strip()


def parse_duration(text: str, default_seconds: Optional[int] = None) -> Tuple[Optional[int], str]:
    """Parse 10m/2h/1d plus Persian forms like 10 دقیقه / 2 ساعت / 1 روز."""
    text = fa_norm(text)
    if not text:
        return (now_ts() + default_seconds if default_seconds else None), ""
    parts = text.split(maxsplit=1)
    m = re.match(r"^(\d+)([smhd])$", parts[0], re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return now_ts() + seconds, parts[1].strip() if len(parts) > 1 else ""
    m = re.match(r"^(\d+)\s*(ثانیه|ثانیه ای|دقیقه|دقیقه ای|ساعت|ساعته|روز|روزه)\s*(.*)$", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        rest = m.group(3).strip()
        if unit.startswith("ثانیه"):
            seconds = n
        elif unit.startswith("دقیقه"):
            seconds = n * 60
        elif unit.startswith("ساعت") or unit == "ساعته":
            seconds = n * 3600
        else:
            seconds = n * 86400
        return now_ts() + seconds, rest
    return (now_ts() + default_seconds if default_seconds else None), text


def is_bot_owner(uid: Optional[int]) -> bool:
    return bool(uid and (uid in OWNER_IDS or db.is_bot_admin(uid)))


def get_group_admin_ids(group_id: int | str) -> set[int]:
    now = time.time()
    key = int(group_id) if str(group_id).lstrip("-").isdigit() else hash(str(group_id))
    cached = admin_cache.get(key)
    if cached and now - cached[0] < 60:
        return cached[1]
    admins = set()
    for cm in api.get_chat_administrators(group_id):
        user = cm.get("user") or cm.get("member") or {}
        uid = user_id(user)
        if uid:
            admins.add(uid)
    admin_cache[key] = (now, admins)
    return admins


def is_group_admin(group_id: int | str, uid: Optional[int]) -> bool:
    if not uid:
        return False
    if is_bot_owner(uid):
        return True
    try:
        return uid in get_group_admin_ids(group_id)
    except Exception:
        return False


def require_admin(msg: dict) -> bool:
    cid = chat_id(msg)
    uid = user_id(msg_from(msg))
    if cid and is_group_admin(cid, uid):
        return True
    reply_text(cid, "⛔ این دستور فقط برای مدیرهای گروه/ربات مجاز است.", msg)
    return False


def panel_keyboard(group_id: Optional[int] = None) -> dict:
    return inline_keyboard([
        [btn("🔒 قفل‌ها", "panel:locks"), btn("👋 خوشامد", "panel:welcome")],
        [btn("📌 قوانین", "panel:rules"), btn("📊 آمار", "panel:stats")],
        [btn("📢 جوین اجباری", "panel:forcejoin"), btn("💾 بکاپ", "panel:backup")],
        [btn("📋 کپی راهنما", copy_text="راهنما")],
    ])


def locks_text(settings: dict) -> str:
    def s(k): return "روشن ✅" if settings.get(k) else "خاموش ❌"
    return (
        "🔒 وضعیت قفل‌ها:\n\n"
        f"لینک: {s('lock_links')}\n"
        f"منشن: {s('lock_mentions')}\n"
        f"مدیا: {s('lock_media')}\n"
        f"استیکر: {s('lock_stickers')}\n"
        f"فوروارد: {s('lock_forwards')}\n"
        f"ضدفلود: {s('anti_flood_enabled')}\n"
    )


def help_text() -> str:
    return (
        "🤖 راهنمای فارسی ربات مدیریت گروه بله\n\n"
        "✅ دستورات اصلی بدون اسلش هم کار می‌کنند. مثال: قفل لینک\n\n"
        "🔒 قفل‌ها:\n"
        "قفل لینک | باز کردن لینک\n"
        "قفل منشن | باز کردن منشن\n"
        "قفل مدیا | باز کردن مدیا\n"
        "قفل استیکر | باز کردن استیکر\n"
        "قفل فوروارد | باز کردن فوروارد\n"
        "قفل فلود | باز کردن فلود\n"
        "قفل همه | باز کردن همه\n\n"
        "👮 مدیریت کاربر، با ریپلای یا آیدی عددی:\n"
        "بن\n"
        "رفع بن 123456\n"
        "اخراج\n"
        "اخطار دلیل\n"
        "حذف اخطار\n"
        "اخطارها\n"
        "میوت 10 دقیقه دلیل\n"
        "رفع میوت\n\n"
        "👋 خوشامد و قوانین:\n"
        "تنظیم خوشامد سلام {first_name} عزیز\n"
        "خوشامد روشن | خوشامد خاموش\n"
        "تنظیم قوانین متن قوانین گروه\n"
        "قوانین\n\n"
        "📢 جوین اجباری:\n"
        "جوین اجباری افزودن @channel عنوان\n"
        "جوین اجباری حذف @channel\n"
        "جوین اجباری لیست\n\n"
        "🧩 فیلتر و نوت:\n"
        "افزودن فیلتر سلام | سلام عزیز\n"
        "حذف فیلتر سلام\n"
        "فیلترها\n"
        "افزودن نوت راهنما | متن راهنما\n"
        "حذف نوت راهنما\n"
        "نوت راهنما\n"
        "نوت‌ها\n\n"
        "🛠 ادمین ربات:\n"
        "افزودن ادمین 123456\n"
        "حذف ادمین 123456\n"
        "ادمین‌ها\n\n"
        "📊 سایر:\n"
        "پنل | آمار | بکاپ | آیدی | راهنما\n\n"
        "دستورات انگلیسی قدیمی مثل /lock links هم هنوز فعال هستند."
    )


def handle_start(msg: dict, args: str) -> None:
    cid = chat_id(msg)
    text = (
        "سلام 🌹\n"
        "این نسخه بازطراحی‌شده ربات مدیریت گروه برای بله است.\n\n"
        "برای مدیریت گروه، ربات را ادمین کن و در گروه دستور «پنل» یا «راهنما» را بزن."
    )
    api.safe_send_message(cid, text, reply_markup=panel_keyboard())


def handle_panel(msg: dict, args: str) -> None:
    cid = chat_id(msg)
    if is_group(msg) and not require_admin(msg):
        return
    api.safe_send_message(cid, "پنل مدیریت ربات:", reply_markup=panel_keyboard(cid))


def handle_id(msg: dict, args: str) -> None:
    cid = chat_id(msg)
    user = msg_from(msg)
    reply = msg.get("reply_to_message") or {}
    target = reply.get("from") or user
    uid = user_id(target)
    text = f"🆔 Chat ID:\n<code>{cid}</code>\n\n👤 User ID:\n<code>{uid}</code>"
    api.safe_send_message(cid, text, reply_to_message_id=message_id(msg), parse_mode="HTML", reply_markup=inline_keyboard([[btn("کپی آیدی کاربر", copy_text=str(uid or ''))]]))


def handle_admins(msg: dict, cmd: str, args: str) -> None:
    cid = chat_id(msg)
    actor = user_id(msg_from(msg))
    if not is_bot_owner(actor):
        api.safe_send_message(cid, "⛔ فقط مالک/ادمین اصلی ربات می‌تواند ادمین‌های ربات را مدیریت کند.", reply_to_message_id=message_id(msg))
        return
    if cmd == "addadmin":
        target, _ = get_target_from_reply_or_arg(msg, args)
        if not target:
            api.safe_send_message(cid, "آیدی عددی را بده یا پیام کاربر را فوروارد کن.\nمثال: /addadmin 123456", reply_to_message_id=message_id(msg))
            return
        db.add_bot_admin(target, actor or 0)
        api.safe_send_message(cid, f"✅ کاربر {target} به ادمین‌های ربات اضافه شد.", reply_to_message_id=message_id(msg))
    elif cmd == "deladmin":
        target, _ = get_target_from_reply_or_arg(msg, args)
        if not target:
            api.safe_send_message(cid, "مثال: /deladmin 123456", reply_to_message_id=message_id(msg))
            return
        if target in OWNER_IDS:
            api.safe_send_message(cid, "مالک اصلی از داخل ربات حذف نمی‌شود؛ OWNER_IDS را از .env تغییر بده.", reply_to_message_id=message_id(msg))
            return
        ok = db.remove_bot_admin(target)
        api.safe_send_message(cid, "✅ حذف شد." if ok else "این آیدی در ادمین‌های ربات نبود.", reply_to_message_id=message_id(msg))
    else:
        admins = db.list_bot_admins()
        lines = ["👮 ادمین‌های ربات:"]
        for a in admins:
            lines.append(f"- {a['user_id']} | {jalali_date(a['created_at'])}")
        api.safe_send_message(cid, "\n".join(lines) if len(lines) > 1 else "ادمینی ثبت نشده.", reply_to_message_id=message_id(msg))


def handle_lock(msg: dict, cmd: str, args: str) -> None:
    if not require_admin(msg):
        return
    cid = int(chat_id(msg))
    target = _normalize_lock_target(args or "")
    mapping = {
        "links": "lock_links", "link": "lock_links", "لینک": "lock_links",
        "mentions": "lock_mentions", "mention": "lock_mentions", "منشن": "lock_mentions", "تگ": "lock_mentions", "یوزرنیم": "lock_mentions",
        "media": "lock_media", "مدیا": "lock_media", "رسانه": "lock_media", "عکس": "lock_media", "فیلم": "lock_media", "ویدیو": "lock_media", "فایل": "lock_media", "ویس": "lock_media", "صدا": "lock_media",
        "stickers": "lock_stickers", "sticker": "lock_stickers", "استیکر": "lock_stickers",
        "forwards": "lock_forwards", "forward": "lock_forwards", "فوروارد": "lock_forwards", "فروارد": "lock_forwards",
        "flood": "anti_flood_enabled", "فلود": "anti_flood_enabled", "اسپم": "anti_flood_enabled",
        "service": "delete_service_messages", "سرویس": "delete_service_messages", "ورود خروج": "delete_service_messages", "ورود و خروج": "delete_service_messages",
    }
    val = cmd == "lock"
    if target in {"all", "همه"}:
        for k in set(mapping.values()):
            db.update_group_setting(cid, k, val)
        api.safe_send_message(cid, f"✅ همه قفل‌ها {'روشن' if val else 'خاموش'} شدند.", reply_to_message_id=message_id(msg))
        return
    if target not in mapping:
        api.safe_send_message(cid, "گزینه درست نیست. مثال: قفل لینک یا باز کردن مدیا", reply_to_message_id=message_id(msg))
        return
    db.update_group_setting(cid, mapping[target], val)
    api.safe_send_message(cid, f"✅ {target} {'قفل شد' if val else 'باز شد'}.", reply_to_message_id=message_id(msg))


def handle_welcome_rules(msg: dict, cmd: str, args: str) -> None:
    cid = int(chat_id(msg))
    if cmd in {"setwelcome", "welcome", "setrules"} and not require_admin(msg):
        return
    if cmd == "setwelcome":
        if not args:
            api.safe_send_message(cid, "متن خوشامد را بعد از دستور بنویس. متغیرها: {first_name} {user_id} {group_title}", reply_to_message_id=message_id(msg))
            return
        db.update_group_setting(cid, "welcome_text", args)
        db.update_group_setting(cid, "welcome_enabled", True)
        api.safe_send_message(cid, "✅ متن خوشامد ذخیره شد.", reply_to_message_id=message_id(msg))
    elif cmd == "welcome":
        a = fa_norm(args).lower().strip()
        on_words = {"on", "روشن", "فعال", "فعال کن", "روشن کن"}
        off_words = {"off", "خاموش", "غیرفعال", "غیر فعال", "خاموش کن"}
        if a not in on_words | off_words:
            api.safe_send_message(cid, "مثال: خوشامد روشن یا خوشامد خاموش", reply_to_message_id=message_id(msg))
            return
        enabled = a in on_words
        db.update_group_setting(cid, "welcome_enabled", enabled)
        api.safe_send_message(cid, f"✅ خوشامد {'روشن' if enabled else 'خاموش'} شد.", reply_to_message_id=message_id(msg))
    elif cmd == "setrules":
        if not args:
            api.safe_send_message(cid, "متن قوانین را بعد از دستور بنویس.", reply_to_message_id=message_id(msg))
            return
        db.update_group_setting(cid, "rules_text", args)
        api.safe_send_message(cid, "✅ قوانین ذخیره شد.", reply_to_message_id=message_id(msg))
    elif cmd == "rules":
        settings = db.get_group_settings(cid)
        api.safe_send_message(cid, "📌 قوانین گروه:\n\n" + settings.get("rules_text", "تنظیم نشده."), reply_to_message_id=message_id(msg))


def handle_moderation(msg: dict, cmd: str, args: str) -> None:
    if not require_admin(msg):
        return
    cid = int(chat_id(msg))
    actor = user_id(msg_from(msg))
    target, reason = get_target_from_reply_or_arg(msg, args)
    if not target:
        api.safe_send_message(cid, "روی پیام کاربر ریپلای کن یا آیدی عددی بده.", reply_to_message_id=message_id(msg))
        return
    if is_group_admin(cid, target):
        api.safe_send_message(cid, "⛔ روی مدیرهای گروه این عملیات انجام نمی‌شود.", reply_to_message_id=message_id(msg))
        return
    reason = reason or "بدون توضیح"
    try:
        if cmd == "ban":
            api.ban_chat_member(cid, target)
            db.log(cid, target, "ban", {"admin_id": actor, "reason": reason})
            api.safe_send_message(cid, f"⛔ کاربر {target} بن شد.\nدلیل: {reason}", reply_to_message_id=message_id(msg))
        elif cmd == "unban":
            api.unban_chat_member(cid, target, only_if_banned=True)
            db.log(cid, target, "unban", {"admin_id": actor})
            api.safe_send_message(cid, f"✅ کاربر {target} آزاد شد.", reply_to_message_id=message_id(msg))
        elif cmd == "kick":
            api.ban_chat_member(cid, target)
            time.sleep(0.4)
            api.unban_chat_member(cid, target, only_if_banned=False)
            db.log(cid, target, "kick", {"admin_id": actor, "reason": reason})
            api.safe_send_message(cid, f"👢 کاربر {target} از گروه حذف شد.\nدلیل: {reason}", reply_to_message_id=message_id(msg))
        elif cmd == "warn":
            count = db.warn_user(cid, target, reason, actor)
            limit = int(db.get_group_settings(cid).get("warn_limit", 3))
            if count >= limit:
                api.ban_chat_member(cid, target)
                db.clear_warnings(cid, target)
                api.safe_send_message(cid, f"⛔ کاربر {target} به حد اخطار رسید و بن شد.", reply_to_message_id=message_id(msg))
            else:
                api.safe_send_message(cid, f"⚠️ اخطار ثبت شد: {count}/{limit}\nدلیل: {reason}", reply_to_message_id=message_id(msg))
        elif cmd == "unwarn":
            db.clear_warnings(cid, target)
            api.safe_send_message(cid, f"✅ اخطارهای کاربر {target} پاک شد.", reply_to_message_id=message_id(msg))
        elif cmd == "warnings":
            data = db.get_warnings(cid, target)
            lines = [f"⚠️ اخطارهای {target}: {data['count']}"]
            for item in data["reasons"][-5:]:
                lines.append(f"- {item.get('date','')} | {item.get('reason','')}")
            api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg))
        elif cmd == "mute":
            until_ts, rem = parse_duration(reason)
            db.mute_user(cid, target, until_ts, rem or "میوت توسط مدیر")
            until_text = jalali_date(until_ts) if until_ts else "نامحدود"
            api.safe_send_message(cid, f"🔇 کاربر {target} میوت شد.\nتا: {until_text}\nپیام‌های بعدی او حذف می‌شود.", reply_to_message_id=message_id(msg))
        elif cmd == "unmute":
            ok = db.unmute_user(cid, target)
            api.safe_send_message(cid, "✅ رفع میوت شد." if ok else "این کاربر میوت نبود.", reply_to_message_id=message_id(msg))
    except BaleAPIError as exc:
        api.safe_send_message(cid, f"❌ عملیات انجام نشد: {exc.description}\nاحتمالاً ربات دسترسی ادمین کافی ندارد.", reply_to_message_id=message_id(msg))


def handle_forcejoin(msg: dict, args: str) -> None:
    if not require_admin(msg):
        return
    cid = int(chat_id(msg))
    parts = args.split(maxsplit=2)
    if not parts:
        api.safe_send_message(cid, "مثال:\nجوین اجباری افزودن @channel عنوان\nجوین اجباری حذف @channel\nجوین اجباری لیست", reply_to_message_id=message_id(msg))
        return
    action = fa_norm(parts[0]).lower()
    action_map = {"افزودن": "add", "اضافه": "add", "اضافهکردن": "add", "اضافه کردن": "add", "حذف": "del", "پاک": "del", "برداشتن": "del", "لیست": "list", "نمایش": "list"}
    action = action_map.get(action, action)
    if action == "add" and len(parts) >= 2:
        channel = parts[1].strip()
        title = parts[2].strip() if len(parts) >= 3 else channel
        db.add_required_channel(cid, channel, title, channel if channel.startswith("http") else "")
        api.safe_send_message(cid, "✅ کانال جوین اجباری اضافه شد. بررسی عضویت در هر تعامل تازه انجام می‌شود.", reply_to_message_id=message_id(msg))
    elif action == "del" and len(parts) >= 2:
        ok = db.remove_required_channel(cid, parts[1].strip())
        api.safe_send_message(cid, "✅ حذف شد." if ok else "کانال پیدا نشد.", reply_to_message_id=message_id(msg))
    elif action == "list":
        chans = db.list_required_channels(cid)
        if not chans:
            api.safe_send_message(cid, "کانالی ثبت نشده.", reply_to_message_id=message_id(msg))
            return
        lines = ["📢 کانال‌های جوین اجباری:"]
        for c in chans:
            lines.append(f"- {c['channel_id']} | {c.get('title') or ''}")
        api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg))
    else:
        api.safe_send_message(cid, "دستور نامعتبر است.", reply_to_message_id=message_id(msg))


def handle_filter_note(msg: dict, cmd: str, args: str) -> None:
    cid = int(chat_id(msg))
    first_action = fa_norm(args.split(maxsplit=1)[0]).lower() if args.split(maxsplit=1) else ""
    if cmd in {"filter", "note"} and first_action in {"add", "del", "افزودن", "اضافه", "حذف", "پاک"}:
        if not require_admin(msg):
            return
    if cmd == "filter":
        parts = args.split(maxsplit=1)
        if not parts:
            api.safe_send_message(cid, "مثال: افزودن فیلتر سلام | سلام عزیز", reply_to_message_id=message_id(msg)); return
        action = fa_norm(parts[0]).lower(); rest = parts[1] if len(parts) > 1 else ""
        action_map = {"افزودن": "add", "اضافه": "add", "اضافه کردن": "add", "حذف": "del", "پاک": "del", "لیست": "list", "نمایش": "list"}
        action = action_map.get(action, action)
        if action == "add":
            if "|" not in rest:
                api.safe_send_message(cid, "فرمت درست: افزودن فیلتر کلمه | پاسخ", reply_to_message_id=message_id(msg)); return
            trigger, response = [x.strip() for x in rest.split("|", 1)]
            db.add_filter(cid, trigger, response)
            api.safe_send_message(cid, "✅ فیلتر ذخیره شد.", reply_to_message_id=message_id(msg))
        elif action == "del":
            ok = db.remove_filter(cid, rest.strip())
            api.safe_send_message(cid, "✅ حذف شد." if ok else "پیدا نشد.", reply_to_message_id=message_id(msg))
        elif action == "list":
            rows = db.list_filters(cid)
            api.safe_send_message(cid, "فیلترها:\n" + "\n".join([f"- {r['trigger']}" for r in rows]) if rows else "فیلتر ندارید.", reply_to_message_id=message_id(msg))
    elif cmd == "filters":
        rows = db.list_filters(cid)
        api.safe_send_message(cid, "فیلترها:\n" + "\n".join([f"- {r['trigger']}" for r in rows]) if rows else "فیلتر ندارید.", reply_to_message_id=message_id(msg))
    elif cmd == "note":
        parts = args.split(maxsplit=1)
        if not parts:
            api.safe_send_message(cid, "مثال: /note add راهنما | متن\nیا /note راهنما", reply_to_message_id=message_id(msg)); return
        action = fa_norm(parts[0]).lower(); rest = parts[1] if len(parts) > 1 else ""
        action_map = {"افزودن": "add", "اضافه": "add", "اضافه کردن": "add", "حذف": "del", "پاک": "del"}
        action = action_map.get(action, action)
        if action == "add":
            if "|" not in rest:
                api.safe_send_message(cid, "فرمت درست: افزودن نوت نام | متن", reply_to_message_id=message_id(msg)); return
            name, text = [x.strip() for x in rest.split("|", 1)]
            db.add_note(cid, name, text)
            api.safe_send_message(cid, "✅ نوت ذخیره شد.", reply_to_message_id=message_id(msg))
        elif action == "del":
            ok = db.remove_note(cid, rest.strip())
            api.safe_send_message(cid, "✅ حذف شد." if ok else "پیدا نشد.", reply_to_message_id=message_id(msg))
        else:
            text = db.get_note(cid, args.strip())
            api.safe_send_message(cid, text or "نوت پیدا نشد.", reply_to_message_id=message_id(msg))
    elif cmd == "notes":
        names = db.list_notes(cid)
        api.safe_send_message(cid, "نوت‌ها:\n" + "\n".join([f"- {n}" for n in names]) if names else "نوتی ندارید.", reply_to_message_id=message_id(msg))


def handle_stats_backup(msg: dict, cmd: str) -> None:
    cid = chat_id(msg)
    if cmd == "stats":
        if is_group(msg) and not require_admin(msg):
            return
        stats = db.group_stats(int(cid)) if is_group(msg) else {}
        if not stats:
            api.safe_send_message(cid, "آمار در چت خصوصی نمایش داده نمی‌شود.", reply_to_message_id=message_id(msg)); return
        lines = [
            "📊 آمار گروه:",
            f"پیام‌های ثبت‌شده: {stats['total_msgs']}",
            f"کاربران فعال: {stats['active_users']}",
            f"مجموع اخطارها: {stats['warnings']}",
            f"میوت‌شده‌ها: {stats['muted']}",
            "",
            "برترین کاربران:",
        ]
        for u in stats["top"]:
            name = u.get("first_name") or u.get("username") or u.get("user_id")
            lines.append(f"- {name}: {u['msg_count']}")
        api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg))
    elif cmd == "backup":
        uid = user_id(msg_from(msg))
        if not is_bot_owner(uid):
            api.safe_send_message(cid, "⛔ بکاپ فقط برای مالک/ادمین اصلی ربات مجاز است.", reply_to_message_id=message_id(msg)); return
        backup = db.backup_zip("backups")
        try:
            api.send_document(cid, backup, caption=f"💾 بکاپ ربات\nتاریخ: {jalali_date()}")
        except Exception as exc:
            api.safe_send_message(cid, f"بکاپ ساخته شد ولی ارسال نشد:\n{backup}\nخطا: {exc}", reply_to_message_id=message_id(msg))


def handle_payment_updates(update: dict) -> bool:
    # Optional Bale wallet payment plumbing. The source keeps it ready but disabled unless PAYMENTS_ENABLED=1.
    pcq = update.get("pre_checkout_query")
    if pcq:
        api.answer_pre_checkout_query(pcq.get("id"), ok=bool(PAYMENTS_ENABLED and BALE_PROVIDER_TOKEN), error_message="پرداخت در این ربات فعال نیست.")
        return True
    msg = update.get("message") or {}
    if msg.get("successful_payment"):
        pay = msg.get("successful_payment") or {}
        db.log(chat_id(msg), user_id(msg_from(msg)), "successful_payment", pay)
        api.safe_send_message(chat_id(msg), "✅ پرداخت با موفقیت ثبت شد.", reply_to_message_id=message_id(msg))
        return True
    return False


def check_force_join(msg: dict, settings: dict) -> bool:
    """Return True when user is allowed. Always checks fresh, no permanent membership cache."""
    if not settings.get("force_join_enabled"):
        return True
    cid = int(chat_id(msg))
    user = msg_from(msg)
    uid = user_id(user)
    if not uid or is_group_admin(cid, uid):
        return True
    channels = db.list_required_channels(cid)
    if not channels:
        return True
    missing = []
    for ch in channels:
        member = api.get_chat_member(ch["channel_id"], uid)
        status = (member or {}).get("status") or ""
        if not member or status.lower() in {"left", "kicked", "banned"}:
            missing.append(ch)
    if not missing:
        return True
    api.safe_delete_message(cid, message_id(msg))
    rows = []
    for ch in missing[:8]:
        title = ch.get("title") or ch["channel_id"]
        link = ch.get("invite_link") or (ch["channel_id"] if str(ch["channel_id"]).startswith("http") else None)
        rows.append([btn(f"عضویت در {title}", url=link) if link else btn(f"{title}", copy_text=str(ch["channel_id"]))])
    rows.append([btn("بعد از عضویت دوباره پیام بده", copy_text="عضو شدم")])
    api.safe_send_message(cid, f"🚫 {first_name(user)} برای ارسال پیام باید اول عضو کانال‌های اجباری شود.", reply_markup=inline_keyboard(rows))
    return False


def process_service_message(msg: dict, settings: dict) -> bool:
    cid = chat_id(msg)
    mid = message_id(msg)
    new_members = msg.get("new_chat_members") or []
    left_member = msg.get("left_chat_member")
    if settings.get("delete_service_messages") and (new_members or left_member):
        api.safe_delete_message(cid, mid)
    if new_members and settings.get("welcome_enabled"):
        for member in new_members:
            text = settings.get("welcome_text") or "سلام {first_name} عزیز"
            data = {
                "first_name": first_name(member),
                "user_id": user_id(member),
                "username": username(member),
                "group_title": db.get_group_title(int(cid)),
                "date": jalali_date(),
            }
            try:
                text = text.format(**data)
            except Exception:
                pass
            api.safe_send_message(cid, text)
    return bool(new_members or left_member)


def moderate_message(msg: dict, settings: dict) -> bool:
    """Return True if message has been handled/deleted."""
    cid = int(chat_id(msg))
    mid = message_id(msg)
    user = msg_from(msg)
    uid = user_id(user)
    if not uid or is_group_admin(cid, uid):
        return False

    if db.is_muted(cid, uid):
        api.safe_delete_message(cid, mid)
        return True

    text = get_text(msg)
    violations = []
    if settings.get("lock_links") and LINK_RE.search(text):
        violations.append("ارسال لینک")
    if settings.get("lock_mentions") and MENTION_RE.search(text):
        violations.append("ارسال منشن")
    if settings.get("lock_media") and has_media(msg):
        violations.append("ارسال مدیا")
    if settings.get("lock_stickers") and has_sticker(msg):
        violations.append("ارسال استیکر")
    if settings.get("lock_forwards") and is_forwarded(msg):
        violations.append("ارسال فوروارد")

    # Anti flood: fresh RAM window; enforcement does not depend on old/cached membership.
    if settings.get("anti_flood_enabled"):
        key = (cid, uid)
        q = flood_cache[key]
        t = now_ts()
        window = int(settings.get("flood_window") or DEFAULT_FLOOD_WINDOW)
        limit = int(settings.get("flood_limit") or DEFAULT_FLOOD_LIMIT)
        q.append(t)
        while q and t - q[0] > window:
            q.popleft()
        if len(q) > limit:
            violations.append("فلود")

    if violations:
        api.safe_delete_message(cid, mid)
        reason = "، ".join(violations)
        count = db.warn_user(cid, uid, reason, None)
        limit = int(settings.get("warn_limit", 3))
        if count >= limit:
            api.safe_call("banChatMember", {"chat_id": cid, "user_id": uid})
            db.clear_warnings(cid, uid)
            api.safe_send_message(cid, f"⛔ {first_name(user)} به دلیل تکرار تخلف بن شد.\nدلیل آخر: {reason}")
        else:
            api.safe_send_message(cid, f"⚠️ {first_name(user)} پیام حذف شد.\nدلیل: {reason}\nاخطار: {count}/{limit}")
        return True
    return False


def handle_normal_group_message(msg: dict) -> None:
    cid = int(chat_id(msg))
    ctitle = msg_chat(msg).get("title") or ""
    settings = db.ensure_group(cid, ctitle)
    db.record_activity(cid, msg_from(msg))

    if process_service_message(msg, settings):
        return
    if not check_force_join(msg, settings):
        return
    if moderate_message(msg, settings):
        return

    text = get_text(msg)
    if not text:
        return
    # Notes by #name or /note name are handled as commands; #note is quick access.
    if text.startswith("#") and len(text) > 1:
        note = db.get_note(cid, text[1:].strip())
        if note:
            api.safe_send_message(cid, note, reply_to_message_id=message_id(msg))
            return
    response = db.match_filter(cid, text)
    if response:
        api.safe_send_message(cid, response, reply_to_message_id=message_id(msg))


def handle_command(msg: dict, cmd: str, args: str) -> None:
    cid = chat_id(msg)
    if is_group(msg):
        db.ensure_group(int(cid), msg_chat(msg).get("title") or "")
    if cmd in {"start"}:
        handle_start(msg, args)
    elif cmd in {"help", "راهنما"}:
        api.safe_send_message(cid, help_text(), reply_to_message_id=message_id(msg), reply_markup=panel_keyboard())
    elif cmd in {"panel", "settings", "پنل"}:
        handle_panel(msg, args)
    elif cmd == "id":
        handle_id(msg, args)
    elif cmd in {"addadmin", "deladmin", "admins"}:
        handle_admins(msg, cmd, args)
    elif cmd in {"lock", "unlock"} and is_group(msg):
        handle_lock(msg, cmd, args)
    elif cmd in {"setwelcome", "welcome", "setrules", "rules"} and is_group(msg):
        handle_welcome_rules(msg, cmd, args)
    elif cmd in {"ban", "unban", "kick", "warn", "unwarn", "warnings", "mute", "unmute"} and is_group(msg):
        handle_moderation(msg, cmd, args)
    elif cmd == "forcejoin" and is_group(msg):
        handle_forcejoin(msg, args)
    elif cmd in {"filter", "filters", "note", "notes"} and is_group(msg):
        handle_filter_note(msg, cmd, args)
    elif cmd in {"stats", "backup"}:
        handle_stats_backup(msg, cmd)
    else:
        # Unknown commands are ignored to keep group clean.
        pass


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query")
    if not q:
        return False
    qid = q.get("id") or q.get("callback_query_id")
    data = q.get("data") or ""
    msg = q.get("message") or {}
    cid = chat_id(msg)
    actor = user_id(q.get("from") or {})

    if data.startswith("panel:"):
        if cid and is_group(msg) and not is_group_admin(cid, actor):
            if qid:
                api.answer_callback_query(qid, "فقط مدیرها مجاز هستند.", True)
            return True

        # Answer callback exactly once. Double-answering the same query can produce
        # 'query ID is invalid' and flood logs.
        if qid:
            api.answer_callback_query(qid, "")

        section = data.split(":", 1)[1]
        if section == "locks" and cid:
            settings = db.get_group_settings(int(cid))
            api.safe_send_message(cid, locks_text(settings))
        elif section == "stats" and cid:
            fake = {"chat": {"id": cid, "type": "group"}, "from": {"id": actor}, "message_id": message_id(msg)}
            handle_stats_backup(fake, "stats")
        elif section == "backup" and cid:
            fake = {"chat": {"id": cid, "type": "group"}, "from": {"id": actor}, "message_id": message_id(msg)}
            handle_stats_backup(fake, "backup")
        elif section == "welcome":
            api.safe_send_message(cid, "برای تنظیم خوشامد:\n/setwelcome سلام {first_name} عزیز")
        elif section == "rules":
            api.safe_send_message(cid, "برای تنظیم قوانین:\n/setrules متن قوانین گروه")
        elif section == "forcejoin":
            api.safe_send_message(cid, "برای جوین اجباری:\n/forcejoin add @channel عنوان\n/forcejoin list")
        return True

    if qid:
        api.answer_callback_query(qid, "")
    return True




# ============================================================
# Extended Persian lock system + inline glass panel toggles
# Added after the original handlers so these names override the basic version.
# ============================================================

recent_join_cache: Dict[Tuple[int, int], int] = {}
duplicate_cache: Dict[Tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))

BALE_LINK_RE = re.compile(r"(?i)(ble\.ir/|bale\.ai/)")
TELEGRAM_LINK_RE = re.compile(r"(?i)(t\.me/|telegram\.me/|telegram\.dog/)")
INSTAGRAM_LINK_RE = re.compile(r"(?i)(instagram\.com/|instagr\.am/)")
WHATSAPP_LINK_RE = re.compile(r"(?i)(wa\.me/|whatsapp\.com/)")
SITE_LINK_RE = re.compile(r"(?i)(https?://|www\.)")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?98|0)?9\d{9}(?!\d)")
CHANNEL_RE = re.compile(r"(?i)(t\.me/|telegram\.me/|ble\.ir/|bale\.ai/|@\w{4,})")
AD_RE = re.compile(r"(?i)(خرید|فروش|تبلیغ|درآمد|کسب\s*درآمد|ثبت\s*نام|عضو\s*شو|جوین\s*شو|join|subscribe|کانال|چنل|پیج|فالو|دایرکت|ارزان|تخفیف|پشتیبانی|سفارش)")
STRETCHED_RE = re.compile(r"(.)\1{5,}")
EMOJI_RE = re.compile("["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]", flags=re.UNICODE)

# قابل تنظیم از .env، مثال:
# BAD_WORDS=کلمه۱,کلمه۲,کلمه۳
BAD_WORDS = [x.strip() for x in os.getenv("BAD_WORDS", "").split(",") if x.strip()]

LOCK_DEFINITIONS = {
    "core": [
        ("lock_links", "لینک", ["لینک", "لینکها", "لینک ها", "links", "link"]),
        ("lock_mentions", "منشن", ["منشن", "تگ", "mentions", "mention"]),
        ("lock_usernames", "آیدی/یوزرنیم", ["آیدی", "ایدی", "یوزرنیم", "نام کاربری", "username", "usernames"]),
        ("lock_forwards", "فوروارد", ["فوروارد", "فروارد", "forward", "forwards"]),
        ("lock_chat", "چت", ["چت", "گفتگو", "گفت و گو", "chat"]),
    ],
    "media": [
        ("lock_media", "همه رسانه", ["مدیا", "رسانه", "media"]),
        ("lock_photos", "عکس", ["عکس", "تصویر", "photo", "photos"]),
        ("lock_videos", "ویدیو", ["ویدیو", "فیلم", "video", "videos"]),
        ("lock_voice", "ویس", ["ویس", "voice"]),
        ("lock_audio", "موزیک/صدا", ["صدا", "آهنگ", "اهنگ", "موزیک", "audio"]),
        ("lock_files", "فایل", ["فایل", "سند", "داکیومنت", "document", "file", "files"]),
        ("lock_stickers", "استیکر", ["استیکر", "sticker", "stickers"]),
        ("lock_gifs", "گیف", ["گیف", "gif", "gifs", "animation"]),
        ("lock_contacts", "مخاطب", ["مخاطب", "شماره مخاطب", "contact", "contacts"]),
        ("lock_locations", "لوکیشن", ["لوکیشن", "موقعیت", "location", "locations"]),
    ],
    "ads": [
        ("lock_ads", "تبلیغ", ["تبلیغ", "تبلیغات", "ad", "ads"]),
        ("lock_channels", "کانال", ["کانال", "چنل", "channel", "channels"]),
        ("lock_phone_numbers", "شماره", ["شماره", "موبایل", "تلفن", "phone", "number"]),
        ("lock_bale_links", "لینک بله", ["لینک بله", "بله", "bale"]),
        ("lock_telegram_links", "لینک تلگرام", ["لینک تلگرام", "تلگرام", "telegram"]),
        ("lock_instagram_links", "لینک اینستاگرام", ["لینک اینستاگرام", "اینستاگرام", "instagram"]),
        ("lock_whatsapp_links", "لینک واتساپ", ["لینک واتساپ", "واتساپ", "whatsapp"]),
        ("lock_site_links", "لینک سایت", ["لینک سایت", "سایت", "وبسایت", "website", "site"]),
    ],
    "spam": [
        ("anti_flood_enabled", "فلود", ["فلود", "اسپم", "ضد فلود", "flood", "spam"]),
        ("lock_duplicate_messages", "پیام تکراری", ["پیام تکراری", "تکراری", "duplicate"]),
        ("lock_long_text", "متن طولانی", ["متن طولانی", "طولانی", "long text"]),
        ("lock_short_text", "پیام کوتاه", ["پیام کوتاه", "متن کوتاه", "short text"]),
        ("lock_emoji_spam", "ایموجی زیاد", ["ایموجی زیاد", "ایموجی", "emoji"]),
        ("lock_stretched_chars", "حروف کشیده", ["حروف کشیده", "کشیده", "تکرار حرف", "stretched"]),
        ("lock_bad_words", "فحش", ["فحش", "بددهنی", "badword", "bad words"]),
    ],
    "security": [
        ("lock_bots", "ربات", ["ربات", "بات", "bot", "bots"]),
        ("lock_fake_accounts", "اکانت فیک", ["اکانت فیک", "فیک", "fake"]),
        ("lock_suspicious_names", "اسم مشکوک", ["اسم مشکوک", "نام مشکوک", "suspicious name"]),
        ("lock_new_members", "عضو جدید", ["عضو جدید", "تازه وارد", "new member", "new members"]),
    ],
    "group": [
        ("delete_service_messages", "پیام سرویس", ["سرویس", "پیام سرویس", "service"]),
        ("lock_join_messages", "پیام ورود", ["پیام ورود", "ورود", "join message"]),
        ("lock_leave_messages", "پیام خروج", ["پیام خروج", "خروج", "leave message"]),
        ("lock_title_change", "تغییر عنوان", ["تغییر عنوان", "تغییر نام گروه", "title change"]),
        ("lock_photo_change", "تغییر عکس گروه", ["تغییر عکس", "عکس گروه", "photo change"]),
        ("lock_night", "قفل شبانه", ["شبانه", "قفل شبانه", "night"]),
    ],
}

LOCK_CATEGORIES = {
    "core": "اصلی",
    "media": "رسانه",
    "ads": "تبلیغات",
    "spam": "ضداسپم",
    "security": "امنیتی",
    "group": "گروه",
}

ALL_LOCK_KEYS = [item[0] for items in LOCK_DEFINITIONS.values() for item in items]
LOCK_NAME_BY_KEY = {item[0]: item[1] for items in LOCK_DEFINITIONS.values() for item in items}
LOCK_ALIAS_TO_KEY = {}
for _cat, _items in LOCK_DEFINITIONS.items():
    for _key, _name, _aliases in _items:
        LOCK_ALIAS_TO_KEY[fa_norm(_name)] = _key
        for _alias in _aliases:
            LOCK_ALIAS_TO_KEY[fa_norm(_alias)] = _key

# Override previous aliases so Persian plain commands like "قفل عکس" stay specific.
LOCK_TARGET_ALIASES = {alias: key for alias, key in LOCK_ALIAS_TO_KEY.items()}
LOCK_TARGET_ALIASES.update({
    "همه": "all", "همه قفلها": "all", "همه قفل ها": "all", "all": "all",
})


def _normalize_lock_target(text: str) -> str:
    t = fa_norm(text)
    t = re.sub(r"^(را|رو|را هم|رو هم)\s+", "", t).strip()
    return LOCK_TARGET_ALIASES.get(t, t)


def _lock_label(settings: dict, key: str) -> str:
    return ("✅ " if settings.get(key) else "❌ ") + LOCK_NAME_BY_KEY.get(key, key)


def _locks_status_lines(settings: dict, category: Optional[str] = None) -> list[str]:
    cats = [category] if category else list(LOCK_DEFINITIONS.keys())
    lines = []
    for cat in cats:
        lines.append(f"\n🔹 {LOCK_CATEGORIES.get(cat, cat)}")
        for key, name, _ in LOCK_DEFINITIONS[cat]:
            lines.append(f"{'✅' if settings.get(key) else '❌'} {name}")
    return lines


def locks_text(settings: dict, category: Optional[str] = None) -> str:
    title = "🔒 وضعیت قفل‌های گروه"
    if category:
        title += f" — {LOCK_CATEGORIES.get(category, category)}"
    return title + "\n" + "\n".join(_locks_status_lines(settings, category))


def locks_panel_keyboard(settings: dict, category: str = "core") -> dict:
    items = LOCK_DEFINITIONS.get(category, LOCK_DEFINITIONS["core"])
    rows = []
    temp = []
    for key, _name, _aliases in items:
        temp.append(btn(_lock_label(settings, key), f"locktoggle:{category}:{key}"))
        if len(temp) == 2:
            rows.append(temp); temp = []
    if temp:
        rows.append(temp)
    rows.append([btn("✅ روشن کردن همه", f"lockall:{category}:1"), btn("❌ خاموش کردن همه", f"lockall:{category}:0")])
    rows.append([btn("اصلی", "locks:core"), btn("رسانه", "locks:media"), btn("تبلیغات", "locks:ads")])
    rows.append([btn("ضداسپم", "locks:spam"), btn("امنیتی", "locks:security"), btn("گروه", "locks:group")])
    rows.append([btn("🔒 قفل همه", "lockall:all:1"), btn("🔓 باز کردن همه", "lockall:all:0")])
    rows.append([btn("⬅️ برگشت", "panel:main")])
    return inline_keyboard(rows)


def panel_keyboard(group_id: Optional[int] = None) -> dict:
    return inline_keyboard([
        [btn("🔒 قفل‌ها", "locks:core"), btn("👋 خوشامد", "panel:welcome")],
        [btn("📌 قوانین", "panel:rules"), btn("📊 آمار", "panel:stats")],
        [btn("📢 جوین اجباری", "panel:forcejoin"), btn("💾 بکاپ", "panel:backup")],
        [btn("📋 کپی راهنما", copy_text="راهنما")],
    ])


def help_text() -> str:
    return (
        "🤖 راهنمای فارسی ربات مدیریت گروه بله\n\n"
        "✅ همه دستورات بدون اسلش هم کار می‌کنند. مثال: قفل لینک\n"
        "✅ برای خاموش/روشن کردن شیشه‌ای هم داخل گروه بزن: پنل\n\n"
        "🔒 نمونه قفل‌ها:\n"
        "قفل لینک | باز کردن لینک\n"
        "قفل عکس | باز کردن عکس\n"
        "قفل ویدیو | باز کردن ویدیو\n"
        "قفل ویس | باز کردن ویس\n"
        "قفل فایل | باز کردن فایل\n"
        "قفل استیکر | باز کردن استیکر\n"
        "قفل فوروارد | باز کردن فوروارد\n"
        "قفل تبلیغ | باز کردن تبلیغ\n"
        "قفل شماره | باز کردن شماره\n"
        "قفل فلود | باز کردن فلود\n"
        "قفل پیام تکراری | باز کردن پیام تکراری\n"
        "قفل متن طولانی | باز کردن متن طولانی\n"
        "قفل فحش | باز کردن فحش\n"
        "قفل ربات | باز کردن ربات\n"
        "قفل شبانه | باز کردن شبانه\n"
        "قفل همه | باز کردن همه\n\n"
        "👮 مدیریت کاربر، با ریپلای یا آیدی عددی:\n"
        "بن | رفع بن | اخراج | اخطار دلیل | حذف اخطار | اخطارها\n"
        "میوت 10 دقیقه دلیل | رفع میوت\n\n"
        "👋 خوشامد و قوانین:\n"
        "تنظیم خوشامد سلام {first_name} عزیز\n"
        "خوشامد روشن | خوشامد خاموش\n"
        "تنظیم قوانین متن قوانین گروه\nقوانین\n\n"
        "📢 جوین اجباری:\n"
        "جوین اجباری افزودن @channel عنوان\nجوین اجباری حذف @channel\nجوین اجباری لیست\n\n"
        "🧩 فیلتر و نوت:\n"
        "افزودن فیلتر سلام | سلام عزیز\nحذف فیلتر سلام\nفیلترها\n"
        "افزودن نوت راهنما | متن راهنما\nنوت راهنما\nنوت‌ها\n\n"
        "📊 سایر: پنل | آمار | بکاپ | آیدی | راهنما"
    )


def _message_has_photo(msg: dict) -> bool:
    return "photo" in msg


def _message_has_video(msg: dict) -> bool:
    return "video" in msg or "video_note" in msg


def _message_has_voice(msg: dict) -> bool:
    return "voice" in msg


def _message_has_audio(msg: dict) -> bool:
    return "audio" in msg


def _message_has_file(msg: dict) -> bool:
    return "document" in msg


def _message_has_gif(msg: dict) -> bool:
    return "animation" in msg


def _message_has_contact(msg: dict) -> bool:
    return "contact" in msg


def _message_has_location(msg: dict) -> bool:
    return "location" in msg or "venue" in msg


def _is_fake_account(user: dict) -> bool:
    # محافظه‌کارانه: فقط وقتی اطلاعات خیلی کم است. پیش‌فرض خاموش است.
    fn = (first_name(user) or "").strip()
    un = username(user)
    return (not un) and (len(fn) <= 2 or fn in {"کاربر", "User", "user"})


def _is_suspicious_name(user: dict) -> bool:
    name = f"{first_name(user)} {user.get('last_name') or user.get('lastName') or ''} {username(user)}"
    return bool(LINK_RE.search(name) or AD_RE.search(name))


def _has_bad_word(text: str) -> bool:
    if not BAD_WORDS:
        return False
    nt = fa_norm(text).lower()
    return any(fa_norm(w).lower() in nt for w in BAD_WORDS)


def _is_night_locked(settings: dict) -> bool:
    try:
        import datetime
        h = datetime.datetime.now().hour
        start = int(settings.get("night_start_hour", 0))
        end = int(settings.get("night_end_hour", 8))
        if start <= end:
            return start <= h < end
        return h >= start or h < end
    except Exception:
        return False


def _record_duplicate_and_check(cid: int, uid: int, text: str) -> bool:
    if not text:
        return False
    norm = fa_norm(text).lower()
    if len(norm) < 3:
        return False
    q = duplicate_cache[(cid, uid)]
    t = now_ts()
    q.append((t, norm))
    recent = [x for ts, x in q if t - ts <= 45]
    return recent.count(norm) >= 3


def handle_lock(msg: dict, cmd: str, args: str) -> None:
    if not require_admin(msg):
        return
    cid = int(chat_id(msg))
    target = _normalize_lock_target(args or "")
    val = cmd == "lock"
    if target in {"all", "همه"}:
        for k in ALL_LOCK_KEYS:
            db.update_group_setting(cid, k, val)
        api.safe_send_message(cid, f"✅ همه قفل‌ها {'روشن' if val else 'خاموش'} شدند.", reply_to_message_id=message_id(msg), reply_markup=locks_panel_keyboard(db.get_group_settings(cid)))
        return
    key = LOCK_ALIAS_TO_KEY.get(fa_norm(target), target)
    if key not in ALL_LOCK_KEYS:
        api.safe_send_message(cid, "گزینه درست نیست. مثال: قفل لینک، قفل عکس، قفل تبلیغ، قفل فلود، قفل همه", reply_to_message_id=message_id(msg))
        return
    db.update_group_setting(cid, key, val)
    name = LOCK_NAME_BY_KEY.get(key, target)
    api.safe_send_message(cid, f"✅ {name} {'قفل شد' if val else 'باز شد'}.", reply_to_message_id=message_id(msg), reply_markup=locks_panel_keyboard(db.get_group_settings(cid), _find_lock_category(key)))


def _find_lock_category(key: str) -> str:
    for cat, items in LOCK_DEFINITIONS.items():
        if any(k == key for k, _, _ in items):
            return cat
    return "core"


def process_service_message(msg: dict, settings: dict) -> bool:
    cid = int(chat_id(msg))
    mid = message_id(msg)
    user = msg_from(msg)
    new_members = msg.get("new_chat_members") or []
    left_member = msg.get("left_chat_member")

    if new_members:
        for member in new_members:
            uid = user_id(member)
            if uid:
                recent_join_cache[(cid, uid)] = now_ts()
            if settings.get("lock_bots") and member.get("is_bot") and uid:
                api.safe_call("banChatMember", {"chat_id": cid, "user_id": uid})

    if settings.get("delete_service_messages") and (new_members or left_member):
        api.safe_delete_message(cid, mid)
    if settings.get("lock_join_messages") and new_members:
        api.safe_delete_message(cid, mid)
    if settings.get("lock_leave_messages") and left_member:
        api.safe_delete_message(cid, mid)
    if settings.get("lock_title_change") and msg.get("new_chat_title"):
        api.safe_delete_message(cid, mid)
        return True
    if settings.get("lock_photo_change") and (msg.get("new_chat_photo") or msg.get("delete_chat_photo")):
        api.safe_delete_message(cid, mid)
        return True

    if new_members and settings.get("welcome_enabled"):
        for member in new_members:
            text = settings.get("welcome_text") or "سلام {first_name} عزیز"
            data = {
                "first_name": first_name(member),
                "user_id": user_id(member),
                "username": username(member),
                "group_title": db.get_group_title(int(cid)),
                "date": jalali_date(),
            }
            try:
                text = text.format(**data)
            except Exception:
                pass
            api.safe_send_message(cid, text)
    return bool(new_members or left_member or msg.get("new_chat_title") or msg.get("new_chat_photo") or msg.get("delete_chat_photo"))


def moderate_message(msg: dict, settings: dict) -> bool:
    cid = int(chat_id(msg))
    mid = message_id(msg)
    user = msg_from(msg)
    uid = user_id(user)
    if not uid or is_group_admin(cid, uid):
        return False

    if db.is_muted(cid, uid):
        api.safe_delete_message(cid, mid)
        return True

    text = get_text(msg) or ""
    violations = []

    if settings.get("lock_chat"):
        violations.append("قفل چت")
    if settings.get("lock_night") and _is_night_locked(settings):
        violations.append("قفل شبانه")
    if settings.get("lock_new_members"):
        joined_at = recent_join_cache.get((cid, uid))
        if joined_at and now_ts() - joined_at <= int(settings.get("new_member_watch_seconds", 300)):
            violations.append("محدودیت عضو جدید")
    if settings.get("lock_fake_accounts") and _is_fake_account(user):
        violations.append("اکانت فیک")
    if settings.get("lock_suspicious_names") and _is_suspicious_name(user):
        violations.append("اسم مشکوک")

    if settings.get("lock_links") and LINK_RE.search(text):
        violations.append("ارسال لینک")
    if settings.get("lock_bale_links") and BALE_LINK_RE.search(text):
        violations.append("ارسال لینک بله")
    if settings.get("lock_telegram_links") and TELEGRAM_LINK_RE.search(text):
        violations.append("ارسال لینک تلگرام")
    if settings.get("lock_instagram_links") and INSTAGRAM_LINK_RE.search(text):
        violations.append("ارسال لینک اینستاگرام")
    if settings.get("lock_whatsapp_links") and WHATSAPP_LINK_RE.search(text):
        violations.append("ارسال لینک واتساپ")
    if settings.get("lock_site_links") and SITE_LINK_RE.search(text):
        violations.append("ارسال لینک سایت")
    if settings.get("lock_channels") and CHANNEL_RE.search(text):
        violations.append("ارسال کانال/یوزرنیم")
    if settings.get("lock_mentions") and MENTION_RE.search(text):
        violations.append("ارسال منشن")
    if settings.get("lock_usernames") and MENTION_RE.search(text):
        violations.append("ارسال آیدی/یوزرنیم")
    if settings.get("lock_phone_numbers") and PHONE_RE.search(normalize_digits(text)):
        violations.append("ارسال شماره")
    if settings.get("lock_ads") and (AD_RE.search(text) and (LINK_RE.search(text) or MENTION_RE.search(text) or PHONE_RE.search(normalize_digits(text)))):
        violations.append("تبلیغات")

    if settings.get("lock_media") and has_media(msg):
        violations.append("ارسال رسانه")
    if settings.get("lock_photos") and _message_has_photo(msg):
        violations.append("ارسال عکس")
    if settings.get("lock_videos") and _message_has_video(msg):
        violations.append("ارسال ویدیو")
    if settings.get("lock_voice") and _message_has_voice(msg):
        violations.append("ارسال ویس")
    if settings.get("lock_audio") and _message_has_audio(msg):
        violations.append("ارسال موزیک/صدا")
    if settings.get("lock_files") and _message_has_file(msg):
        violations.append("ارسال فایل")
    if settings.get("lock_stickers") and has_sticker(msg):
        violations.append("ارسال استیکر")
    if settings.get("lock_gifs") and _message_has_gif(msg):
        violations.append("ارسال گیف")
    if settings.get("lock_contacts") and _message_has_contact(msg):
        violations.append("ارسال مخاطب")
    if settings.get("lock_locations") and _message_has_location(msg):
        violations.append("ارسال لوکیشن")
    if settings.get("lock_forwards") and is_forwarded(msg):
        violations.append("ارسال فوروارد")

    if settings.get("lock_duplicate_messages") and _record_duplicate_and_check(cid, uid, text):
        violations.append("پیام تکراری")
    if settings.get("lock_long_text") and len(text) > int(settings.get("long_text_limit", 500)):
        violations.append("متن طولانی")
    if settings.get("lock_short_text") and text and len(fa_norm(text)) <= int(settings.get("short_text_limit", 2)):
        violations.append("پیام کوتاه")
    if settings.get("lock_emoji_spam") and len(EMOJI_RE.findall(text)) > int(settings.get("emoji_limit", 10)):
        violations.append("ایموجی زیاد")
    if settings.get("lock_stretched_chars") and STRETCHED_RE.search(text):
        violations.append("حروف کشیده/تکراری")
    if settings.get("lock_bad_words") and _has_bad_word(text):
        violations.append("فحش")

    if settings.get("anti_flood_enabled"):
        key = (cid, uid)
        q = flood_cache[key]
        t = now_ts()
        window = int(settings.get("flood_window") or DEFAULT_FLOOD_WINDOW)
        limit = int(settings.get("flood_limit") or DEFAULT_FLOOD_LIMIT)
        q.append(t)
        while q and t - q[0] > window:
            q.popleft()
        if len(q) > limit:
            violations.append("فلود")

    if violations:
        api.safe_delete_message(cid, mid)
        reason = "، ".join(dict.fromkeys(violations))
        count = db.warn_user(cid, uid, reason, None)
        limit = int(settings.get("warn_limit", 3))
        if count >= limit:
            api.safe_call("banChatMember", {"chat_id": cid, "user_id": uid})
            db.clear_warnings(cid, uid)
            api.safe_send_message(cid, f"⛔ {first_name(user)} به دلیل تکرار تخلف بن شد.\nدلیل آخر: {reason}")
        else:
            api.safe_send_message(cid, f"⚠️ {first_name(user)} پیام حذف شد.\nدلیل: {reason}\nاخطار: {count}/{limit}")
        return True
    return False


def _edit_or_send_panel(cid, mid, text, keyboard):
    if cid and mid:
        ok = api.safe_call("editMessageText", {"chat_id": cid, "message_id": mid, "text": text[:4096], "reply_markup": keyboard}, default=None)
        if ok:
            return ok
    return api.safe_send_message(cid, text, reply_markup=keyboard)


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query")
    if not q:
        return False
    qid = q.get("id") or q.get("callback_query_id")
    data = q.get("data") or ""
    msg = q.get("message") or {}
    cid = chat_id(msg)
    mid = message_id(msg)
    actor = user_id(q.get("from") or {})

    if cid and is_group(msg) and not is_group_admin(cid, actor):
        if qid:
            api.answer_callback_query(qid, "فقط مدیرهای گروه مجاز هستند.", True)
        return True

    if qid:
        api.answer_callback_query(qid, "")

    if data == "panel:main":
        _edit_or_send_panel(cid, mid, "پنل مدیریت ربات:", panel_keyboard(cid))
        return True

    if data.startswith("locks:") and cid:
        category = data.split(":", 1)[1] or "core"
        settings = db.get_group_settings(int(cid))
        _edit_or_send_panel(cid, mid, locks_text(settings, category), locks_panel_keyboard(settings, category))
        return True

    if data.startswith("locktoggle:") and cid:
        _, category, key = data.split(":", 2)
        if key in ALL_LOCK_KEYS:
            settings = db.get_group_settings(int(cid))
            new_value = not bool(settings.get(key))
            db.update_group_setting(int(cid), key, new_value)
            settings = db.get_group_settings(int(cid))
            _edit_or_send_panel(cid, mid, locks_text(settings, category), locks_panel_keyboard(settings, category))
        return True

    if data.startswith("lockall:") and cid:
        _, category, value = data.split(":", 2)
        val = value == "1"
        keys = ALL_LOCK_KEYS if category == "all" else [k for k, _, _ in LOCK_DEFINITIONS.get(category, [])]
        for k in keys:
            db.update_group_setting(int(cid), k, val)
        settings = db.get_group_settings(int(cid))
        show_cat = "core" if category == "all" else category
        _edit_or_send_panel(cid, mid, locks_text(settings, show_cat), locks_panel_keyboard(settings, show_cat))
        return True

    if data.startswith("panel:"):
        section = data.split(":", 1)[1]
        if section == "locks" and cid:
            settings = db.get_group_settings(int(cid))
            _edit_or_send_panel(cid, mid, locks_text(settings, "core"), locks_panel_keyboard(settings, "core"))
        elif section == "stats" and cid:
            fake = {"chat": {"id": cid, "type": "group"}, "from": {"id": actor}, "message_id": mid}
            handle_stats_backup(fake, "stats")
        elif section == "backup" and cid:
            fake = {"chat": {"id": cid, "type": "group"}, "from": {"id": actor}, "message_id": mid}
            handle_stats_backup(fake, "backup")
        elif section == "welcome":
            api.safe_send_message(cid, "برای تنظیم خوشامد:\nتنظیم خوشامد سلام {first_name} عزیز\nخوشامد روشن | خوشامد خاموش")
        elif section == "rules":
            api.safe_send_message(cid, "برای تنظیم قوانین:\nتنظیم قوانین متن قوانین گروه")
        elif section == "forcejoin":
            api.safe_send_message(cid, "برای جوین اجباری:\nجوین اجباری افزودن @channel عنوان\nجوین اجباری حذف @channel\nجوین اجباری لیست")
        return True

    return True

def process_update(update: dict) -> None:
    try:
        if handle_payment_updates(update):
            return
        if handle_callback(update):
            return
        msg = get_message(update)
        if not msg:
            return
        text = get_text(msg)
        command = get_command(text)
        if command:
            handle_command(msg, command[0], command[1])
            return
        if is_group(msg):
            handle_normal_group_message(msg)
    except Exception:
        log.exception("Update processing failed: %s", json.dumps(update, ensure_ascii=False)[:1500])



# ============================================================
# Pro management pack: friendly Persian UX, warnings, logs, stats,
# force-join, welcome/rules, roles, backups, premium, payments, anti-raid.
# This block intentionally overrides selected handlers above.
# ============================================================
import datetime

CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "").strip()
CARD_NOTE = os.getenv("CARD_NOTE", "بعد از واریز، عکس رسید رو بفرست و بنویس: رسید").strip()
FREE_GROUPS_ENABLED = os.getenv("FREE_GROUPS_ENABLED", "1") == "1"
DEFAULT_PLAN_DAYS = int(os.getenv("DEFAULT_PLAN_DAYS", "30"))
PLAN_PRICE_IRR = int(os.getenv("PLAN_PRICE_IRR", "99000"))
AUTO_BACKUP_ENABLED = os.getenv("AUTO_BACKUP_ENABLED", "0") == "1"
AUTO_BACKUP_HOURS = int(os.getenv("AUTO_BACKUP_HOURS", "24"))

PRO_DEFAULTS = {
    "goodbye_enabled": False,
    "goodbye_text": "فعلاً {first_name} 👋",
    "rules_enabled": True,
    "rules_must_accept": False,
    "force_join_text": "رفیق اول باید عضو کانال‌های زیر بشی، بعد دوباره پیام بده 👇",
    "warn_action": "mute",          # none | mute | kick | ban
    "warn_mute_minutes": 60,
    "log_enabled": False,
    "log_chat_id": None,
    "anti_raid_enabled": False,
    "premium_enabled": False,
    "premium_expires_at": None,
    "auto_backup_enabled": AUTO_BACKUP_ENABLED,
    "auto_backup_hours": AUTO_BACKUP_HOURS,
}

ROLE_TITLES = {
    "owner": "مالک گروه",
    "all": "همه‌کاره",
    "locks": "مدیر قفل‌ها",
    "warns": "مدیر اخطار",
    "welcome": "مدیر خوشامد",
    "forcejoin": "مدیر جوین",
    "filters": "مدیر فیلترها",
    "stats": "مدیر آمار",
    "logs": "مدیر لاگ",
    "premium": "مدیر اشتراک",
}
ROLE_ALIASES = {
    "مالک": "owner", "مالک گروه": "owner",
    "همه": "all", "همه کاره": "all", "همه‌کاره": "all", "مدیرکل": "all", "مدیر کل": "all",
    "قفل": "locks", "قفلها": "locks", "قفل ها": "locks", "قفل‌ها": "locks",
    "اخطار": "warns", "هشدار": "warns", "مدیریت کاربر": "warns",
    "خوشامد": "welcome", "قوانین": "welcome", "خوش آمد": "welcome",
    "جوین": "forcejoin", "جوین اجباری": "forcejoin", "عضویت اجباری": "forcejoin",
    "فیلتر": "filters", "نوت": "filters", "یادداشت": "filters",
    "آمار": "stats", "امار": "stats", "گزارش": "stats",
    "لاگ": "logs", "گزارش مدیریتی": "logs",
    "اشتراک": "premium", "پرمیوم": "premium", "پرداخت": "premium",
}
ROLE_PERMS = {
    "owner": {"all"},
    "all": {"all"},
    "locks": {"locks", "panel"},
    "warns": {"warns", "panel"},
    "welcome": {"welcome", "rules", "panel"},
    "forcejoin": {"forcejoin", "panel"},
    "filters": {"filters", "panel"},
    "stats": {"stats", "panel"},
    "logs": {"logs", "panel"},
    "premium": {"premium", "payments", "panel"},
}

PERM_BY_CALLBACK = {
    "locks": "locks", "locktoggle": "locks", "lockall": "locks",
    "warn": "warns", "logs": "logs", "stats": "stats",
    "force": "forcejoin", "welcome": "welcome", "rules": "rules",
    "roles": "all", "backup": "all", "premium": "premium",
    "payment": "payments", "attack": "locks", "set": "all",
}


def ensure_pro_schema() -> None:
    with db._lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS group_roles (
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            added_by INTEGER,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(group_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS rules_acceptances (
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            accepted_at INTEGER NOT NULL,
            PRIMARY KEY(group_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            group_id INTEGER PRIMARY KEY,
            plan TEXT,
            expires_at INTEGER,
            active INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            user_id INTEGER,
            method TEXT NOT NULL,
            amount INTEGER,
            status TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_activity (
            day TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            msg_count INTEGER NOT NULL DEFAULT 0,
            deleted_count INTEGER NOT NULL DEFAULT 0,
            warn_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(day, group_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_activity_group_day ON daily_activity(group_id, day);
        """)


ensure_pro_schema()


def _settings(cid: int) -> dict:
    st = db.get_group_settings(int(cid))
    merged = PRO_DEFAULTS.copy()
    merged.update(st)
    return merged


def today_key(offset_days: int = 0) -> str:
    d = datetime.datetime.now() + datetime.timedelta(days=offset_days)
    return d.strftime("%Y%m%d")


def _days_ago_key(days: int) -> str:
    d = datetime.datetime.now() - datetime.timedelta(days=days)
    return d.strftime("%Y%m%d")


def ext_record_daily(cid: int, user: dict, msg_delta: int = 0, deleted_delta: int = 0, warn_delta: int = 0) -> None:
    uid = user_id(user) if isinstance(user, dict) else int(user or 0)
    if not uid:
        return
    day = today_key()
    with db._lock:
        db.conn.execute(
            """
            INSERT INTO daily_activity(day,group_id,user_id,msg_count,deleted_count,warn_count)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(day,group_id,user_id) DO UPDATE SET
                msg_count=msg_count+excluded.msg_count,
                deleted_count=deleted_count+excluded.deleted_count,
                warn_count=warn_count+excluded.warn_count
            """,
            (day, int(cid), int(uid), int(msg_delta), int(deleted_delta), int(warn_delta)),
        )


def ext_group_stats(cid: int, days: int = 1) -> dict:
    start = _days_ago_key(days - 1)
    rows = db.conn.execute(
        "SELECT user_id,SUM(msg_count) AS m,SUM(deleted_count) AS d,SUM(warn_count) AS w FROM daily_activity WHERE group_id=? AND day>=? GROUP BY user_id ORDER BY m DESC LIMIT 10",
        (int(cid), start),
    ).fetchall()
    total = db.conn.execute(
        "SELECT COALESCE(SUM(msg_count),0) AS m, COALESCE(SUM(deleted_count),0) AS d, COALESCE(SUM(warn_count),0) AS w FROM daily_activity WHERE group_id=? AND day>=?",
        (int(cid), start),
    ).fetchone()
    base = db.group_stats(int(cid))
    return {"range_days": days, "total": dict(total), "top": [dict(r) for r in rows], "base": base}


def _set_role(cid: int, uid: int, role: str, added_by: Optional[int]) -> None:
    role = ROLE_ALIASES.get(fa_norm(role), role)
    if role not in ROLE_TITLES:
        role = "all"
    with db._lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO group_roles(group_id,user_id,role,added_by,created_at) VALUES(?,?,?,?,?)",
            (int(cid), int(uid), role, int(added_by or 0), now_ts()),
        )
        db.log(cid, uid, "role_set", {"role": role, "added_by": added_by})


def _remove_role(cid: int, uid: int) -> bool:
    with db._lock:
        cur = db.conn.execute("DELETE FROM group_roles WHERE group_id=? AND user_id=?", (int(cid), int(uid)))
        db.log(cid, uid, "role_remove", {})
        return cur.rowcount > 0


def _get_role(cid: int, uid: Optional[int]) -> Optional[str]:
    if not uid:
        return None
    row = db.conn.execute("SELECT role FROM group_roles WHERE group_id=? AND user_id=?", (int(cid), int(uid))).fetchone()
    return row["role"] if row else None


def _list_roles(cid: int) -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM group_roles WHERE group_id=? ORDER BY created_at DESC", (int(cid),)).fetchall()]


def _role_can(role: Optional[str], perm: str) -> bool:
    if not role:
        return False
    perms = ROLE_PERMS.get(role, set())
    return "all" in perms or perm in perms or perm == "panel" and bool(perms)


def can_manage(cid: int | str, uid: Optional[int], perm: str = "all") -> bool:
    if not uid:
        return False
    if is_bot_owner(uid):
        return True
    try:
        if uid in get_group_admin_ids(cid):
            return True
    except Exception:
        pass
    return _role_can(_get_role(int(cid), uid), perm)


def is_group_admin(group_id: int | str, uid: Optional[int]) -> bool:
    if not uid:
        return False
    if can_manage(group_id, uid, "panel"):
        return True
    return False


def require_admin(msg: dict, perm: str = "all") -> bool:
    cid = chat_id(msg)
    uid = user_id(msg_from(msg))
    if cid and can_manage(cid, uid, perm):
        return True
    reply_text(cid, "داداش این بخش برای مدیرهای مجازه 😅", msg)
    return False


def _safe_int(x, default=0):
    try:
        return int(str(x).strip())
    except Exception:
        return default


def send_group_log(cid: int, title: str, body: str = "", user: Optional[dict] = None) -> None:
    st = _settings(int(cid))
    if not st.get("log_enabled"):
        return
    log_cid = st.get("log_chat_id") or cid
    u = user or {}
    uline = ""
    if u:
        uid = user_id(u)
        uline = f"\n👤 کاربر: {first_name(u)} | <code>{uid}</code>"
    text = f"📋 {title}{uline}\n🏷 گروه: {db.get_group_title(int(cid))}\n🕒 {jalali_date()}"
    if body:
        text += f"\n\n{body}"
    api.safe_send_message(log_cid, text, parse_mode="HTML")


def _friendly_punishment_name(action: str) -> str:
    return {"none": "فقط اخطار", "mute": "میوت", "kick": "اخراج", "ban": "بن"}.get(action, action)


def _apply_warn_punishment(cid: int, target: int, reason: str, actor: Optional[int] = None) -> str:
    st = _settings(cid)
    action = str(st.get("warn_action") or "mute")
    try:
        if action == "ban":
            api.ban_chat_member(cid, target)
            db.clear_warnings(cid, target)
            send_group_log(cid, "کاربر بن شد", f"دلیل: {reason}\nاز طرف: {actor or 'ربات'}")
            return "بن شد 🚫"
        if action == "kick":
            api.ban_chat_member(cid, target)
            time.sleep(0.3)
            api.unban_chat_member(cid, target, only_if_banned=False)
            db.clear_warnings(cid, target)
            send_group_log(cid, "کاربر اخراج شد", f"دلیل: {reason}\nاز طرف: {actor or 'ربات'}")
            return "از گروه پرید بیرون 👢"
        if action == "mute":
            minutes = int(st.get("warn_mute_minutes") or 60)
            db.mute_user(cid, target, now_ts() + minutes * 60, f"رسیدن به سقف اخطار | {reason}")
            db.clear_warnings(cid, target)
            send_group_log(cid, "کاربر میوت شد", f"مدت: {minutes} دقیقه\nدلیل: {reason}\nاز طرف: {actor or 'ربات'}")
            return f"برای {minutes} دقیقه میوت شد 🔇"
    except Exception as exc:
        log.warning("warn punishment failed: %s", exc)
    return "فعلاً فقط اخطار خورد ⚠️"


def warn_user_smart(cid: int, uid: int, reason: str, actor: Optional[int], public_name: str = "کاربر") -> str:
    count = db.warn_user(cid, uid, reason, actor)
    ext_record_daily(cid, uid, warn_delta=1)
    limit = int(_settings(cid).get("warn_limit", 3))
    send_group_log(cid, "اخطار ثبت شد", f"برای: <code>{uid}</code>\nدلیل: {reason}\nوضعیت: {count}/{limit}")
    if count >= limit:
        result = _apply_warn_punishment(cid, uid, reason, actor)
        return f"⚠️ {public_name} به سقف اخطار رسید ({count}/{limit}) و {result}"
    return f"⚠️ {public_name} اخطار گرفت.\nدلیل: {reason}\nوضعیت: {count}/{limit}"


def _subscription_status(cid: int) -> dict:
    row = db.conn.execute("SELECT * FROM subscriptions WHERE group_id=?", (int(cid),)).fetchone()
    if not row:
        active = bool(FREE_GROUPS_ENABLED)
        return {"active": active, "plan": "free" if active else "none", "expires_at": None}
    data = dict(row)
    active = bool(data.get("active")) and (not data.get("expires_at") or int(data["expires_at"]) > now_ts())
    data["active"] = active
    return data


def _activate_subscription(cid: int, days: int = DEFAULT_PLAN_DAYS, plan: str = "premium") -> int:
    exp = now_ts() + int(days) * 86400
    with db._lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO subscriptions(group_id,plan,expires_at,active,updated_at) VALUES(?,?,?,?,?)",
            (int(cid), plan, exp, 1, now_ts()),
        )
        db.update_group_setting(cid, "premium_enabled", True)
        db.update_group_setting(cid, "premium_expires_at", exp)
        db.log(cid, None, "subscription_activate", {"days": days, "expires_at": exp, "plan": plan})
    return exp


def _deactivate_subscription(cid: int) -> None:
    with db._lock:
        db.conn.execute("INSERT OR REPLACE INTO subscriptions(group_id,plan,expires_at,active,updated_at) VALUES(?,?,?,?,?)", (int(cid), "none", None, 0, now_ts()))
        db.update_group_setting(cid, "premium_enabled", False)
        db.update_group_setting(cid, "premium_expires_at", None)


def _premium_text(cid: int) -> str:
    sub = _subscription_status(cid)
    if sub["active"]:
        if sub.get("expires_at"):
            return f"💎 اشتراک فعاله تا {jalali_date(int(sub['expires_at']))}"
        return "💎 این گروه فعاله. محدودیتی نداره."
    return "💎 اشتراک این گروه فعال نیست. برای فعال‌سازی از بخش پرداخت استفاده کن."


def _format_main_panel(cid: Optional[int]) -> str:
    title = db.get_group_title(int(cid)) if cid else "چت خصوصی"
    sub = _subscription_status(int(cid)) if cid and str(cid).lstrip('-').isdigit() else {"active": True}
    status = "فعاله ✅" if sub.get("active") else "غیرفعاله ❌"
    return (
        f"🎛 پنل خوشگل مدیریت گروه\n"
        f"📌 گروه: {title}\n"
        f"💎 وضعیت: {status}\n\n"
        "هر چی لازم داری از دکمه‌های پایین بزن؛ متن‌ها کوتاه و واضح چیده شدن 👇"
    )


def panel_keyboard(group_id: Optional[int] = None) -> dict:
    return inline_keyboard([
        [btn("🔒 قفل‌ها", "locks:core"), btn("⚠️ اخطارها", "p:warn")],
        [btn("📋 لاگ‌ها", "p:logs"), btn("📊 آمار", "p:stats")],
        [btn("📢 جوین اجباری", "p:force"), btn("👋 خوشامد/قوانین", "p:welcome")],
        [btn("👮 مدیرها", "p:roles"), btn("💾 بکاپ/ریستور", "p:backup")],
        [btn("💎 اشتراک", "p:premium"), btn("💳 پرداخت", "p:payment")],
        [btn("🚨 ضدحمله", "p:attack"), btn("📋 کپی راهنما", copy_text="راهنما")],
    ])


def handle_panel(msg: dict, args: str) -> None:
    cid = chat_id(msg)
    if is_group(msg) and not require_admin(msg, "panel"):
        return
    api.safe_send_message(cid, _format_main_panel(cid), reply_markup=panel_keyboard(cid))


def help_text() -> str:
    return (
        "🤖 راهنمای ربات\n\n"
        "برای تنظیم راحت، داخل گروه بزن: پنل\n"
        "همه چیز با دکمه شیشه‌ای خاموش/روشن میشه 😎\n\n"
        "چندتا دستور کاربردی:\n"
        "قفل لینک | باز کردن لینک\n"
        "قفل همه | باز کردن همه\n"
        "اخطار دلیل تخلف\n"
        "میوت 10 دقیقه اسپم\n"
        "بن | رفع بن | اخراج\n"
        "لاگ اینجا | لاگ خاموش\n"
        "جوین اجباری افزودن @channel عنوان\n"
        "تنظیم خوشامد سلام {first_name} جان خوش اومدی\n"
        "تنظیم قوانین متن قوانین گروه\n"
        "افزودن مدیر قفل‌ها 123456\n"
        "ضدحمله روشن | ضدحمله خاموش\n"
        "اشتراک وضعیت | پرداخت\n\n"
        "برای جزئیات بیشتر فقط پنل رو باز کن."
    )


def _panel_text(section: str, cid: int) -> str:
    st = _settings(cid)
    if section == "warn":
        return ("⚠️ تنظیمات اخطار\n\n"
                f"سقف اخطار: {st.get('warn_limit', 3)}\n"
                f"بعد از سقف: {_friendly_punishment_name(st.get('warn_action', 'mute'))}\n"
                f"زمان میوت: {st.get('warn_mute_minutes', 60)} دقیقه\n\n"
                "اینجا تعیین می‌کنی کاربر بعد از چند اخطار چی بشه.")
    if section == "logs":
        return ("📋 لاگ مدیریتی\n\n"
                f"وضعیت: {'روشن ✅' if st.get('log_enabled') else 'خاموش ❌'}\n"
                f"چت لاگ: {st.get('log_chat_id') or 'همین گروه / تنظیم نشده'}\n\n"
                "هر حذف پیام، اخطار، بن، تغییر تنظیمات و پرداخت اینجا ثبت میشه.")
    if section == "stats":
        data = ext_group_stats(cid, 7)
        t = data["total"]
        lines = ["📊 آمار گروه", "", f"پیام‌های ۷ روز اخیر: {int(t['m'])}", f"حذف‌شده‌ها: {int(t['d'])}", f"اخطارها: {int(t['w'])}", "", "فعال‌ترین‌ها:"]
        if not data["top"]:
            lines.append("هنوز چیزی ثبت نشده.")
        for r in data["top"][:5]:
            lines.append(f"• {r['user_id']}: {int(r['m'])} پیام")
        return "\n".join(lines)
    if section == "force":
        chans = db.list_required_channels(cid)
        return ("📢 جوین اجباری\n\n"
                f"وضعیت: {'روشن ✅' if st.get('force_join_enabled') else 'خاموش ❌'}\n"
                f"کانال‌های ثبت‌شده: {len(chans)}\n\n"
                "کاربر تا وقتی عضو کانال‌ها نشه، پیامش حذف میشه.")
    if section == "welcome":
        return ("👋 خوشامد و قوانین\n\n"
                f"خوشامد: {'روشن ✅' if st.get('welcome_enabled') else 'خاموش ❌'}\n"
                f"خداحافظی: {'روشن ✅' if st.get('goodbye_enabled') else 'خاموش ❌'}\n"
                f"قوانین: {'روشن ✅' if st.get('rules_enabled') else 'خاموش ❌'}\n"
                f"تأیید قوانین: {'روشن ✅' if st.get('rules_must_accept') else 'خاموش ❌'}")
    if section == "roles":
        roles = _list_roles(cid)
        lines = ["👮 مدیرهای داخل ربات", ""]
        if not roles:
            lines.append("هنوز مدیر داخلی ثبت نشده.")
        for r in roles[:20]:
            lines.append(f"• {r['user_id']} — {ROLE_TITLES.get(r['role'], r['role'])}")
        lines.append("\nبرای افزودن: افزودن مدیر قفل‌ها 123456")
        return "\n".join(lines)
    if section == "backup":
        return "💾 بکاپ و ریستور\n\nبکاپ دستی بگیر، فایل دیتابیس رو نگه دار، یا از مسیر بکاپ ریستور کن."
    if section == "premium":
        return _premium_text(cid) + "\n\nبا اشتراک می‌تونی امکانات حرفه‌ای رو برای گروه فعال نگه داری."
    if section == "payment":
        return ("💳 پرداخت\n\n"
                f"پلن فعلی: {DEFAULT_PLAN_DAYS} روزه\n"
                f"قیمت: {PLAN_PRICE_IRR:,} تومان\n\n"
                "یکی از روش‌های پایین رو انتخاب کن.")
    if section == "attack":
        return ("🚨 حالت ضدحمله\n\n"
                f"وضعیت: {'روشن ✅' if st.get('anti_raid_enabled') else 'خاموش ❌'}\n\n"
                "وقتی گروه اسپم شد، اینو روشن کن؛ قفل‌های حساس خودکار فعال میشن.")
    return _format_main_panel(cid)


def _section_keyboard(section: str, cid: int) -> dict:
    st = _settings(cid)
    if section == "warn":
        return inline_keyboard([
            [btn("➖ سقف", "warn:limit:-"), btn(f"سقف: {st.get('warn_limit',3)}", "noop"), btn("➕ سقف", "warn:limit:+")],
            [btn("فقط اخطار", "warn:action:none"), btn("میوت", "warn:action:mute")],
            [btn("اخراج", "warn:action:kick"), btn("بن", "warn:action:ban")],
            [btn("میوت ۳۰د", "warn:mute:30"), btn("میوت ۶۰د", "warn:mute:60"), btn("میوت ۲۴س", "warn:mute:1440")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "logs":
        return inline_keyboard([
            [btn("✅ روشن", "logs:on"), btn("❌ خاموش", "logs:off")],
            [btn("📍 لاگ همینجا", "logs:sethere"), btn("🧪 تست لاگ", "logs:test")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "stats":
        return inline_keyboard([
            [btn("📅 امروز", "stats:1"), btn("📆 ۷ روز", "stats:7"), btn("🗓 ۳۰ روز", "stats:30")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "force":
        return inline_keyboard([
            [btn("✅ روشن", "force:on"), btn("❌ خاموش", "force:off")],
            [btn("📜 لیست کانال‌ها", "force:list"), btn("🧹 حذف همه", "force:clear")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "welcome":
        return inline_keyboard([
            [btn("خوشامد ✅", "welcome:on"), btn("خوشامد ❌", "welcome:off")],
            [btn("خداحافظی ✅", "goodbye:on"), btn("خداحافظی ❌", "goodbye:off")],
            [btn("قوانین ✅", "rules:on"), btn("قوانین ❌", "rules:off")],
            [btn("تأیید قوانین ✅", "rulesaccept:on"), btn("تأیید قوانین ❌", "rulesaccept:off")],
            [btn("📋 دیدن قوانین", "rules:show"), btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "roles":
        return inline_keyboard([
            [btn("📋 لیست مدیرها", "roles:list")],
            [btn("قفل‌ها", copy_text="افزودن مدیر قفل‌ها 123456"), btn("اخطار", copy_text="افزودن مدیر اخطار 123456")],
            [btn("جوین", copy_text="افزودن مدیر جوین 123456"), btn("همه‌کاره", copy_text="افزودن مدیر همه‌کاره 123456")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "backup":
        return inline_keyboard([
            [btn("💾 ساخت بکاپ", "backup:create"), btn("📜 بکاپ‌های اخیر", "backup:list")],
            [btn("بکاپ خودکار ✅", "backup:auto:on"), btn("بکاپ خودکار ❌", "backup:auto:off")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "premium":
        return inline_keyboard([
            [btn("📌 وضعیت", "premium:status"), btn("➕ فعال‌سازی ۳۰ روز", "premium:add30")],
            [btn("➕ تمدید ۹۰ روز", "premium:add90"), btn("❌ غیرفعال", "premium:off")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "payment":
        return inline_keyboard([
            [btn("💳 پرداخت بله", "payment:bale"), btn("💳 کارت‌به‌کارت", "payment:card")],
            [btn("📷 ثبت رسید", copy_text="رسید"), btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "attack":
        return inline_keyboard([
            [btn("🚨 روشن کن", "attack:on"), btn("🟢 خاموش کن", "attack:off")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    return panel_keyboard(cid)


def _render_section(cid: int, mid: Optional[int], section: str) -> None:
    _edit_or_send_panel(cid, mid, _panel_text(section, cid), _section_keyboard(section, cid))


def _toggle_setting(cid: int, key: str, val: bool) -> None:
    db.update_group_setting(int(cid), key, bool(val))
    send_group_log(int(cid), "تنظیمات عوض شد", f"{key}: {'روشن' if val else 'خاموش'}")


def _enable_anti_raid(cid: int, enabled: bool) -> None:
    keys_on = [
        "anti_raid_enabled", "lock_links", "lock_forwards", "lock_channels", "lock_ads",
        "lock_new_members", "anti_flood_enabled", "lock_duplicate_messages", "lock_stretched_chars",
    ]
    if enabled:
        for k in keys_on:
            db.update_group_setting(cid, k, True)
        db.update_group_setting(cid, "flood_limit", 4)
        db.update_group_setting(cid, "flood_window", 7)
    else:
        db.update_group_setting(cid, "anti_raid_enabled", False)
    send_group_log(cid, "حالت ضدحمله تغییر کرد", "روشن شد 🚨" if enabled else "خاموش شد 🟢")


def _recent_backups(limit: int = 8) -> list[Path]:
    bdir = Path("backups")
    if not bdir.exists():
        return []
    return sorted(bdir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def _maybe_auto_backup() -> None:
    if not AUTO_BACKUP_ENABLED:
        return
    last = _safe_int(db.get_meta("last_auto_backup_ts", "0"), 0)
    if now_ts() - last < AUTO_BACKUP_HOURS * 3600:
        return
    try:
        path = db.backup_zip("backups")
        db.set_meta("last_auto_backup_ts", now_ts())
        log.info("Auto backup created: %s", path)
    except Exception:
        log.exception("Auto backup failed")


def _old_get_command_safe(text: str):
    try:
        return _OLD_GET_COMMAND(text)
    except Exception:
        return None


_OLD_GET_COMMAND = get_command

def get_command(text: str) -> Optional[Tuple[str, str]]:
    raw = text or ""
    t = fa_norm(raw)
    if not t:
        return None

    patterns = [
        (["سقف اخطار", "حد اخطار"], "warnsettings", "limit"),
        (["مجازات اخطار", "تنبیه اخطار"], "warnsettings", "action"),
        (["زمان میوت اخطار", "مدت میوت اخطار"], "warnsettings", "mutetime"),
        (["لاگ اینجا"], "logs", "sethere"),
        (["لاگ خاموش"], "logs", "off"),
        (["لاگ روشن"], "logs", "on"),
        (["تنظیم لاگ"], "logs", "set"),
        (["تنظیم خداحافظی", "متن خداحافظی"], "setgoodbye", ""),
        (["خداحافظی روشن"], "goodbye", "on"),
        (["خداحافظی خاموش"], "goodbye", "off"),
        (["قوانین روشن"], "rules_toggle", "on"),
        (["قوانین خاموش"], "rules_toggle", "off"),
        (["تایید قوانین روشن", "تأیید قوانین روشن"], "rules_accept", "on"),
        (["تایید قوانین خاموش", "تأیید قوانین خاموش"], "rules_accept", "off"),
        (["تنظیم پیام جوین"], "forcejointext", ""),
        (["جوین اجباری روشن"], "forcejoin", "on"),
        (["جوین اجباری خاموش"], "forcejoin", "off"),
        (["افزودن مدیر", "اضافه مدیر", "افزودن ادمین", "اضافه ادمین"], "grouproleadd", ""),
        (["حذف مدیر", "حذف ادمین", "پاک مدیر", "پاک ادمین"], "grouproledel", ""),
        (["مدیرها", "مدیران", "ادمین‌های ربات", "ادمین های ربات"], "grouproles", ""),
        (["اشتراک وضعیت"], "premium", "status"),
        (["اشتراک فعالسازی", "فعال سازی اشتراک", "فعال‌سازی اشتراک"], "premium", "add"),
        (["اشتراک تمدید", "تمدید اشتراک"], "premium", "add"),
        (["اشتراک حذف", "حذف اشتراک"], "premium", "off"),
        (["پرداخت"], "payment", ""),
        (["کارت به کارت", "کارت‌به‌کارت"], "payment", "card"),
        (["رسید"], "receipt", ""),
        (["ضدحمله روشن", "حالت ضدحمله روشن", "حالت اضطراری"], "attack", "on"),
        (["ضدحمله خاموش", "حالت ضدحمله خاموش"], "attack", "off"),
        (["آمار امروز"], "stats", "today"),
        (["آمار هفته"], "stats", "week"),
        (["آمار ماه"], "stats", "month"),
        (["ریستور", "بازیابی"], "restore", ""),
    ]
    for phrases, cmd, fixed in patterns:
        m = _starts(t, phrases)
        if m:
            rest = m[1]
            return cmd, (fixed + (" " + rest if fixed and rest else rest)).strip()
    return _old_get_command_safe(raw)


def handle_warnsettings(msg: dict, args: str) -> None:
    if not require_admin(msg, "warns"):
        return
    cid = int(chat_id(msg))
    a = fa_norm(args)
    if a.startswith("limit"):
        n = _safe_int(a.replace("limit", "").strip(), 3)
        n = min(max(n, 1), 20)
        db.update_group_setting(cid, "warn_limit", n)
        api.safe_send_message(cid, f"اوکی، سقف اخطار شد {n} تا ⚠️", reply_to_message_id=message_id(msg))
    elif a.startswith("action"):
        r = fa_norm(a.replace("action", "").strip())
        amap = {"هیچی": "none", "فقط اخطار": "none", "میوت": "mute", "سکوت": "mute", "اخراج": "kick", "کیک": "kick", "بن": "ban"}
        action = amap.get(r, r if r in {"none", "mute", "kick", "ban"} else "mute")
        db.update_group_setting(cid, "warn_action", action)
        api.safe_send_message(cid, f"حله، بعد از سقف اخطار: {_friendly_punishment_name(action)}", reply_to_message_id=message_id(msg))
    elif a.startswith("mutetime"):
        n = _safe_int(a.replace("mutetime", "").strip(), 60)
        db.update_group_setting(cid, "warn_mute_minutes", min(max(n, 1), 10080))
        api.safe_send_message(cid, f"زمان میوت اخطار شد {n} دقیقه 🔇", reply_to_message_id=message_id(msg))


def handle_logs(msg: dict, args: str) -> None:
    if not require_admin(msg, "logs"):
        return
    cid = int(chat_id(msg))
    a = fa_norm(args)
    if a.startswith("sethere"):
        db.update_group_setting(cid, "log_chat_id", cid)
        db.update_group_setting(cid, "log_enabled", True)
        api.safe_send_message(cid, "لاگ‌ها از این به بعد همینجا میاد 📋", reply_to_message_id=message_id(msg))
    elif a.startswith("set"):
        parts = a.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
            api.safe_send_message(cid, "مثال: تنظیم لاگ -100123456", reply_to_message_id=message_id(msg)); return
        db.update_group_setting(cid, "log_chat_id", int(parts[1]))
        db.update_group_setting(cid, "log_enabled", True)
        api.safe_send_message(cid, "چت لاگ تنظیم شد ✅", reply_to_message_id=message_id(msg))
    elif a == "on":
        db.update_group_setting(cid, "log_enabled", True)
        api.safe_send_message(cid, "لاگ روشن شد ✅", reply_to_message_id=message_id(msg))
    elif a == "off":
        db.update_group_setting(cid, "log_enabled", False)
        api.safe_send_message(cid, "لاگ خاموش شد ❌", reply_to_message_id=message_id(msg))


def handle_welcome_rules(msg: dict, cmd: str, args: str) -> None:
    cid = int(chat_id(msg))
    if cmd in {"setwelcome", "welcome", "setrules", "setgoodbye", "goodbye", "rules_toggle", "rules_accept"} and not require_admin(msg, "welcome"):
        return
    if cmd == "setwelcome":
        if not args:
            api.safe_send_message(cid, "متن خوشامد رو هم بنویس 😄\nمثال: تنظیم خوشامد سلام {first_name} جان", reply_to_message_id=message_id(msg)); return
        db.update_group_setting(cid, "welcome_text", args)
        db.update_group_setting(cid, "welcome_enabled", True)
        api.safe_send_message(cid, "خوشامد ذخیره شد 👋", reply_to_message_id=message_id(msg))
    elif cmd == "setgoodbye":
        if not args:
            api.safe_send_message(cid, "متن خداحافظی رو هم بنویس. مثال: تنظیم خداحافظی فعلاً {first_name} 👋", reply_to_message_id=message_id(msg)); return
        db.update_group_setting(cid, "goodbye_text", args)
        db.update_group_setting(cid, "goodbye_enabled", True)
        api.safe_send_message(cid, "خداحافظی ذخیره شد 👋", reply_to_message_id=message_id(msg))
    elif cmd == "welcome":
        enabled = fa_norm(args) in {"on", "روشن", "فعال", "فعال کن", "روشن کن"}
        db.update_group_setting(cid, "welcome_enabled", enabled)
        api.safe_send_message(cid, f"خوشامد {'روشن شد ✅' if enabled else 'خاموش شد ❌'}", reply_to_message_id=message_id(msg))
    elif cmd == "goodbye":
        enabled = fa_norm(args) in {"on", "روشن", "فعال"}
        db.update_group_setting(cid, "goodbye_enabled", enabled)
        api.safe_send_message(cid, f"خداحافظی {'روشن شد ✅' if enabled else 'خاموش شد ❌'}", reply_to_message_id=message_id(msg))
    elif cmd == "setrules":
        if not args:
            api.safe_send_message(cid, "متن قوانین رو بعد دستور بنویس.", reply_to_message_id=message_id(msg)); return
        db.update_group_setting(cid, "rules_text", args)
        db.update_group_setting(cid, "rules_enabled", True)
        api.safe_send_message(cid, "قوانین ذخیره شد 📌", reply_to_message_id=message_id(msg))
    elif cmd == "rules_toggle":
        enabled = fa_norm(args) == "on"
        db.update_group_setting(cid, "rules_enabled", enabled)
        api.safe_send_message(cid, f"قوانین {'روشن شد ✅' if enabled else 'خاموش شد ❌'}", reply_to_message_id=message_id(msg))
    elif cmd == "rules_accept":
        enabled = fa_norm(args) == "on"
        db.update_group_setting(cid, "rules_must_accept", enabled)
        api.safe_send_message(cid, f"تأیید قوانین {'روشن شد ✅' if enabled else 'خاموش شد ❌'}", reply_to_message_id=message_id(msg))
    elif cmd == "rules":
        st = _settings(cid)
        api.safe_send_message(cid, "📌 قوانین گروه:\n\n" + st.get("rules_text", "هنوز قوانین تنظیم نشده."), reply_to_message_id=message_id(msg), reply_markup=inline_keyboard([[btn("قبول دارم ✅", f"rulesok:{user_id(msg_from(msg)) or 0}")]]))


def handle_grouproles(msg: dict, cmd: str, args: str) -> None:
    if not require_admin(msg, "all"):
        return
    cid = int(chat_id(msg)); actor = user_id(msg_from(msg))
    if cmd == "grouproleadd":
        parts = fa_norm(args).split()
        if len(parts) < 2:
            api.safe_send_message(cid, "مثال: افزودن مدیر قفل‌ها 123456", reply_to_message_id=message_id(msg)); return
        role_raw = " ".join(parts[:-1])
        target = _safe_int(parts[-1], 0)
        if not target:
            target, _ = get_target_from_reply_or_arg(msg, args)
        if not target:
            api.safe_send_message(cid, "آیدی عددی بده یا روی پیام طرف ریپلای کن.", reply_to_message_id=message_id(msg)); return
        role = ROLE_ALIASES.get(fa_norm(role_raw), "all")
        _set_role(cid, target, role, actor)
        api.safe_send_message(cid, f"اوکی، {target} شد {ROLE_TITLES.get(role)} ✅", reply_to_message_id=message_id(msg))
    elif cmd == "grouproledel":
        target, _ = get_target_from_reply_or_arg(msg, args)
        if not target:
            api.safe_send_message(cid, "مثال: حذف مدیر 123456", reply_to_message_id=message_id(msg)); return
        ok = _remove_role(cid, target)
        api.safe_send_message(cid, "حذف شد ✅" if ok else "این کاربر مدیر داخلی نبود.", reply_to_message_id=message_id(msg))
    else:
        api.safe_send_message(cid, _panel_text("roles", cid), reply_to_message_id=message_id(msg), reply_markup=_section_keyboard("roles", cid))


def handle_forcejoin(msg: dict, args: str) -> None:
    if not require_admin(msg, "forcejoin"):
        return
    cid = int(chat_id(msg))
    a = fa_norm(args)
    if a == "on":
        db.update_group_setting(cid, "force_join_enabled", True); api.safe_send_message(cid, "جوین اجباری روشن شد ✅", reply_to_message_id=message_id(msg)); return
    if a == "off":
        db.update_group_setting(cid, "force_join_enabled", False); api.safe_send_message(cid, "جوین اجباری خاموش شد ❌", reply_to_message_id=message_id(msg)); return
    parts = args.split(maxsplit=2)
    if not parts:
        api.safe_send_message(cid, "مثال:\nجوین اجباری افزودن @channel عنوان\nجوین اجباری حذف @channel\nجوین اجباری لیست", reply_to_message_id=message_id(msg)); return
    action = fa_norm(parts[0]).lower()
    action_map = {"افزودن": "add", "اضافه": "add", "حذف": "del", "پاک": "del", "لیست": "list", "نمایش": "list", "clear": "clear", "پاکسازی": "clear"}
    action = action_map.get(action, action)
    if action == "add" and len(parts) >= 2:
        channel = parts[1].strip(); title = parts[2].strip() if len(parts) >= 3 else channel
        db.add_required_channel(cid, channel, title, channel if channel.startswith("http") else "")
        db.update_group_setting(cid, "force_join_enabled", True)
        api.safe_send_message(cid, f"کانال {title} اضافه شد ✅", reply_to_message_id=message_id(msg))
    elif action == "del" and len(parts) >= 2:
        ok = db.remove_required_channel(cid, parts[1].strip())
        api.safe_send_message(cid, "حذف شد ✅" if ok else "پیداش نکردم 😅", reply_to_message_id=message_id(msg))
    elif action == "clear":
        for c in db.list_required_channels(cid):
            db.remove_required_channel(cid, c["channel_id"])
        api.safe_send_message(cid, "همه کانال‌های جوین اجباری پاک شدن 🧹", reply_to_message_id=message_id(msg))
    else:
        chans = db.list_required_channels(cid)
        lines = ["📢 کانال‌های جوین اجباری:"]
        if not chans: lines.append("هیچی ثبت نشده.")
        for c in chans:
            lines.append(f"• {c['channel_id']} — {c.get('title') or ''}")
        api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg))


def handle_forcejointext(msg: dict, args: str) -> None:
    if not require_admin(msg, "forcejoin"):
        return
    cid = int(chat_id(msg))
    if not args:
        api.safe_send_message(cid, "متن پیام جوین اجباری رو بنویس.", reply_to_message_id=message_id(msg)); return
    db.update_group_setting(cid, "force_join_text", args)
    api.safe_send_message(cid, "متن جوین اجباری ذخیره شد ✅", reply_to_message_id=message_id(msg))


def check_force_join(msg: dict, settings: dict) -> bool:
    if not settings.get("force_join_enabled"):
        return True
    cid = int(chat_id(msg)); user = msg_from(msg); uid = user_id(user)
    if not uid or can_manage(cid, uid, "panel"):
        return True
    channels = db.list_required_channels(cid)
    if not channels:
        return True
    missing = []
    for ch in channels:
        member = api.get_chat_member(ch["channel_id"], uid)
        status = (member or {}).get("status") or ""
        if not member or status.lower() in {"left", "kicked", "banned"}:
            missing.append(ch)
    if not missing:
        return True
    api.safe_delete_message(cid, message_id(msg)); ext_record_daily(cid, uid, deleted_delta=1)
    rows = []
    for ch in missing[:8]:
        title = ch.get("title") or str(ch["channel_id"])
        link = ch.get("invite_link") or (ch["channel_id"] if str(ch["channel_id"]).startswith("http") else None)
        rows.append([btn(f"عضویت در {title}", url=link) if link else btn(f"کپی آیدی {title}", copy_text=str(ch["channel_id"]))])
    rows.append([btn("عضو شدم، چک کن ✅", f"fjcheck:{uid}")])
    text = settings.get("force_join_text") or PRO_DEFAULTS["force_join_text"]
    api.safe_send_message(cid, f"{first_name(user)} جان 👋\n{text}", reply_markup=inline_keyboard(rows))
    return False


def process_service_message(msg: dict, settings: dict) -> bool:
    cid = int(chat_id(msg)); mid = message_id(msg)
    new_members = msg.get("new_chat_members") or []
    left_member = msg.get("left_chat_member")
    if new_members:
        for member in new_members:
            uid = user_id(member)
            if uid:
                recent_join_cache[(cid, uid)] = now_ts()
            if settings.get("lock_bots") and member.get("is_bot") and not can_manage(cid, uid, "panel"):
                api.safe_call("banChatMember", {"chat_id": cid, "user_id": uid})
                send_group_log(cid, "ربات جدید حذف شد", f"آیدی: {uid}", member)
                continue
            if settings.get("welcome_enabled"):
                text = settings.get("welcome_text") or PRO_DEFAULTS.get("welcome_text", "سلام {first_name} عزیز")
                data = {"first_name": first_name(member), "user_id": uid, "username": username(member), "group_title": db.get_group_title(cid), "date": jalali_date()}
                try: text = text.format(**data)
                except Exception: pass
                rows = []
                if settings.get("rules_enabled"):
                    rows.append([btn("📌 قوانین گروه", "rules:show")])
                if settings.get("rules_must_accept") and uid:
                    rows.append([btn("قبول دارم ✅", f"rulesok:{uid}")])
                api.safe_send_message(cid, text, reply_markup=inline_keyboard(rows) if rows else None)
    if left_member and settings.get("goodbye_enabled"):
        text = settings.get("goodbye_text") or PRO_DEFAULTS["goodbye_text"]
        data = {"first_name": first_name(left_member), "user_id": user_id(left_member), "username": username(left_member), "group_title": db.get_group_title(cid), "date": jalali_date()}
        try: text = text.format(**data)
        except Exception: pass
        api.safe_send_message(cid, text)
    if settings.get("delete_service_messages") and (new_members or left_member):
        api.safe_delete_message(cid, mid)
    if settings.get("lock_join_messages") and new_members:
        api.safe_delete_message(cid, mid)
    if settings.get("lock_leave_messages") and left_member:
        api.safe_delete_message(cid, mid)
    return bool(new_members or left_member)


def _accepted_rules(cid: int, uid: int) -> bool:
    row = db.conn.execute("SELECT 1 FROM rules_acceptances WHERE group_id=? AND user_id=?", (int(cid), int(uid))).fetchone()
    return bool(row)


def _set_rules_accepted(cid: int, uid: int) -> None:
    db.conn.execute("INSERT OR REPLACE INTO rules_acceptances(group_id,user_id,accepted_at) VALUES(?,?,?)", (int(cid), int(uid), now_ts()))


def moderate_message(msg: dict, settings: dict) -> bool:
    cid = int(chat_id(msg)); mid = message_id(msg); user = msg_from(msg); uid = user_id(user)
    if not uid or can_manage(cid, uid, "panel"):
        return False
    if settings.get("rules_must_accept") and not _accepted_rules(cid, uid):
        api.safe_delete_message(cid, mid); ext_record_daily(cid, uid, deleted_delta=1)
        api.safe_send_message(cid, f"{first_name(user)} اول قوانین رو قبول کن بعد پیام بده 🙂", reply_markup=inline_keyboard([[btn("قبول دارم ✅", f"rulesok:{uid}")]]))
        return True
    if db.is_muted(cid, uid):
        api.safe_delete_message(cid, mid); ext_record_daily(cid, uid, deleted_delta=1)
        return True
    text = get_text(msg); violations = []
    if settings.get("anti_raid_enabled") and recent_join_cache.get((cid, uid)) and now_ts() - recent_join_cache[(cid, uid)] < 600:
        if LINK_RE.search(text) or has_media(msg) or is_forwarded(msg):
            violations.append("عضو تازه‌وارد مشکوک")
    if settings.get("lock_chat") or (settings.get("lock_night") and _is_night_locked(settings)):
        violations.append("چت قفله")
    if settings.get("lock_new_members"):
        joined_at = recent_join_cache.get((cid, uid))
        if joined_at and now_ts() - joined_at <= int(settings.get("new_member_watch_seconds", 300)):
            violations.append("محدودیت عضو جدید")
    if settings.get("lock_fake_accounts") and _is_fake_account(user): violations.append("اکانت فیک")
    if settings.get("lock_suspicious_names") and _is_suspicious_name(user): violations.append("اسم مشکوک")
    if settings.get("lock_links") and LINK_RE.search(text): violations.append("لینک")
    if settings.get("lock_bale_links") and BALE_LINK_RE.search(text): violations.append("لینک بله")
    if settings.get("lock_telegram_links") and TELEGRAM_LINK_RE.search(text): violations.append("لینک تلگرام")
    if settings.get("lock_instagram_links") and INSTAGRAM_LINK_RE.search(text): violations.append("لینک اینستاگرام")
    if settings.get("lock_whatsapp_links") and WHATSAPP_LINK_RE.search(text): violations.append("لینک واتساپ")
    if settings.get("lock_site_links") and SITE_LINK_RE.search(text): violations.append("لینک سایت")
    if settings.get("lock_channels") and CHANNEL_RE.search(text): violations.append("کانال/یوزرنیم")
    if settings.get("lock_mentions") and MENTION_RE.search(text): violations.append("منشن")
    if settings.get("lock_usernames") and MENTION_RE.search(text): violations.append("آیدی/یوزرنیم")
    if settings.get("lock_phone_numbers") and PHONE_RE.search(normalize_digits(text)): violations.append("شماره")
    if settings.get("lock_ads") and (AD_RE.search(text) and (LINK_RE.search(text) or MENTION_RE.search(text) or PHONE_RE.search(normalize_digits(text)))): violations.append("تبلیغ")
    if settings.get("lock_media") and has_media(msg): violations.append("رسانه")
    if settings.get("lock_photos") and _message_has_photo(msg): violations.append("عکس")
    if settings.get("lock_videos") and _message_has_video(msg): violations.append("ویدیو")
    if settings.get("lock_voice") and _message_has_voice(msg): violations.append("ویس")
    if settings.get("lock_audio") and _message_has_audio(msg): violations.append("موزیک/صدا")
    if settings.get("lock_files") and _message_has_file(msg): violations.append("فایل")
    if settings.get("lock_stickers") and has_sticker(msg): violations.append("استیکر")
    if settings.get("lock_gifs") and _message_has_gif(msg): violations.append("گیف")
    if settings.get("lock_contacts") and _message_has_contact(msg): violations.append("مخاطب")
    if settings.get("lock_locations") and _message_has_location(msg): violations.append("لوکیشن")
    if settings.get("lock_forwards") and is_forwarded(msg): violations.append("فوروارد")
    if settings.get("lock_duplicate_messages") and _record_duplicate_and_check(cid, uid, text): violations.append("پیام تکراری")
    if settings.get("lock_long_text") and len(text) > int(settings.get("long_text_limit", 500)): violations.append("متن طولانی")
    if settings.get("lock_short_text") and text and len(fa_norm(text)) <= int(settings.get("short_text_limit", 2)): violations.append("پیام کوتاه")
    if settings.get("lock_emoji_spam") and len(EMOJI_RE.findall(text)) > int(settings.get("emoji_limit", 10)): violations.append("ایموجی زیاد")
    if settings.get("lock_stretched_chars") and STRETCHED_RE.search(text): violations.append("حروف کشیده")
    if settings.get("lock_bad_words") and _has_bad_word(text): violations.append("فحش")
    if settings.get("anti_flood_enabled"):
        key = (cid, uid); q = flood_cache[key]; t = now_ts(); window = int(settings.get("flood_window") or DEFAULT_FLOOD_WINDOW); limit = int(settings.get("flood_limit") or DEFAULT_FLOOD_LIMIT)
        q.append(t)
        while q and t - q[0] > window: q.popleft()
        if len(q) > limit: violations.append("فلود")
    if violations:
        api.safe_delete_message(cid, mid); ext_record_daily(cid, uid, deleted_delta=1)
        reason = "، ".join(dict.fromkeys(violations))
        msg_text = warn_user_smart(cid, uid, reason, None, first_name(user))
        api.safe_send_message(cid, msg_text)
        return True
    return False


def handle_normal_group_message(msg: dict) -> None:
    cid = int(chat_id(msg)); ctitle = msg_chat(msg).get("title") or ""; settings = db.ensure_group(cid, ctitle); settings = _settings(cid)
    db.record_activity(cid, msg_from(msg)); ext_record_daily(cid, msg_from(msg), msg_delta=1)
    if process_service_message(msg, settings): return
    if not check_force_join(msg, settings): return
    if moderate_message(msg, settings): return
    text = get_text(msg)
    if not text: return
    if text.startswith("#") and len(text) > 1:
        note = db.get_note(cid, text[1:].strip())
        if note: api.safe_send_message(cid, note, reply_to_message_id=message_id(msg)); return
    response = db.match_filter(cid, text)
    if response: api.safe_send_message(cid, response, reply_to_message_id=message_id(msg))


def handle_moderation(msg: dict, cmd: str, args: str) -> None:
    if not require_admin(msg, "warns"):
        return
    cid = int(chat_id(msg)); actor = user_id(msg_from(msg)); target, reason = get_target_from_reply_or_arg(msg, args)
    if not target:
        api.safe_send_message(cid, "روی پیام طرف ریپلای کن یا آیدیش رو بده.", reply_to_message_id=message_id(msg)); return
    if can_manage(cid, target, "panel"):
        api.safe_send_message(cid, "روی مدیرهای گروه کاری انجام نمیدم 😅", reply_to_message_id=message_id(msg)); return
    reason = reason or "بدون دلیل"
    try:
        if cmd == "ban":
            api.ban_chat_member(cid, target); db.log(cid, target, "ban", {"admin_id": actor, "reason": reason}); send_group_log(cid, "بن دستی", f"کاربر: <code>{target}</code>\nدلیل: {reason}")
            api.safe_send_message(cid, f"کاربر {target} بن شد 🚫\nدلیل: {reason}", reply_to_message_id=message_id(msg))
        elif cmd == "unban":
            api.unban_chat_member(cid, target, only_if_banned=True); db.log(cid, target, "unban", {"admin_id": actor}); send_group_log(cid, "رفع بن", f"کاربر: <code>{target}</code>")
            api.safe_send_message(cid, f"کاربر {target} آزاد شد ✅", reply_to_message_id=message_id(msg))
        elif cmd == "kick":
            api.ban_chat_member(cid, target); time.sleep(0.3); api.unban_chat_member(cid, target, only_if_banned=False); db.log(cid, target, "kick", {"admin_id": actor, "reason": reason}); send_group_log(cid, "اخراج دستی", f"کاربر: <code>{target}</code>\nدلیل: {reason}")
            api.safe_send_message(cid, f"کاربر {target} از گروه رفت بیرون 👢", reply_to_message_id=message_id(msg))
        elif cmd == "warn":
            api.safe_send_message(cid, warn_user_smart(cid, target, reason, actor, str(target)), reply_to_message_id=message_id(msg))
        elif cmd == "unwarn":
            db.clear_warnings(cid, target); api.safe_send_message(cid, f"اخطارهای {target} پاک شد ✅", reply_to_message_id=message_id(msg))
        elif cmd == "warnings":
            data = db.get_warnings(cid, target); lines = [f"⚠️ اخطارهای {target}: {data['count']}"]
            for item in data["reasons"][-10:]: lines.append(f"• {item.get('date','')} — {item.get('reason','')}")
            api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg))
        elif cmd == "mute":
            until_ts, rem = parse_duration(reason, default_seconds=3600); db.mute_user(cid, target, until_ts, rem or "میوت دستی"); send_group_log(cid, "میوت دستی", f"کاربر: <code>{target}</code>\nتا: {jalali_date(until_ts) if until_ts else 'نامحدود'}")
            api.safe_send_message(cid, f"{target} میوت شد 🔇\nتا: {jalali_date(until_ts) if until_ts else 'نامحدود'}", reply_to_message_id=message_id(msg))
        elif cmd == "unmute":
            ok = db.unmute_user(cid, target); api.safe_send_message(cid, "رفع میوت شد ✅" if ok else "این کاربر میوت نبود.", reply_to_message_id=message_id(msg))
    except BaleAPIError as exc:
        api.safe_send_message(cid, f"انجام نشد 😕\n{exc.description}\nاحتمالاً دسترسی ادمین ربات کافی نیست.", reply_to_message_id=message_id(msg))


def handle_stats_backup(msg: dict, cmd: str) -> None:
    cid = chat_id(msg)
    if cmd == "stats":
        if is_group(msg) and not require_admin(msg, "stats"): return
        days = 7
        text_arg = get_text(msg)
        if "امروز" in text_arg or text_arg.endswith("today"): days = 1
        if "ماه" in text_arg or text_arg.endswith("month"): days = 30
        data = ext_group_stats(int(cid), days); t = data["total"]; base = data["base"]
        lines = [f"📊 آمار {days} روز اخیر", "", f"پیام‌ها: {int(t['m'])}", f"حذف‌شده‌ها: {int(t['d'])}", f"اخطارها: {int(t['w'])}", f"کاربران فعال کل: {base.get('active_users',0)}", "", "🔥 فعال‌ترین‌ها:"]
        if not data["top"]: lines.append("فعلاً داده‌ای ندارم.")
        for r in data["top"]: lines.append(f"• {r['user_id']}: {int(r['m'])} پیام")
        api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg), reply_markup=_section_keyboard("stats", int(cid)))
    elif cmd == "backup":
        uid = user_id(msg_from(msg))
        if not is_bot_owner(uid) and not (is_group(msg) and can_manage(cid, uid, "all")):
            api.safe_send_message(cid, "بکاپ فقط دست مالک یا مدیر همه‌کاره‌ست.", reply_to_message_id=message_id(msg)); return
        backup = db.backup_zip("backups")
        try: api.send_document(cid, backup, caption=f"💾 بکاپ آماده‌ست\n{jalali_date()}")
        except Exception as exc: api.safe_send_message(cid, f"بکاپ ساخته شد ولی ارسال نشد:\n{backup}\nخطا: {exc}", reply_to_message_id=message_id(msg))


def handle_restore(msg: dict, args: str) -> None:
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if not is_bot_owner(uid):
        api.safe_send_message(cid, "ریستور فقط برای مالک اصلی رباته.", reply_to_message_id=message_id(msg)); return
    if not args:
        api.safe_send_message(cid, "مسیر فایل بکاپ رو بده. مثال:\nریستور backups/file.zip", reply_to_message_id=message_id(msg)); return
    try:
        db.restore_from_zip(args.strip())
        api.safe_send_message(cid, "ریستور انجام شد ✅\nربات رو یک بار ری‌استارت کن.", reply_to_message_id=message_id(msg))
    except Exception as exc:
        api.safe_send_message(cid, f"ریستور نشد 😕\n{exc}", reply_to_message_id=message_id(msg))


def handle_premium(msg: dict, args: str) -> None:
    cid = int(chat_id(msg)); uid = user_id(msg_from(msg)); a = fa_norm(args)
    if a.startswith("status") or a.startswith("وضعیت") or not a:
        api.safe_send_message(cid, _premium_text(cid), reply_to_message_id=message_id(msg), reply_markup=_section_keyboard("premium", cid)); return
    if not is_bot_owner(uid):
        api.safe_send_message(cid, "فعال‌سازی اشتراک فقط دست مالک رباته.", reply_to_message_id=message_id(msg)); return
    if a.startswith("off"):
        _deactivate_subscription(cid); api.safe_send_message(cid, "اشتراک این گروه غیرفعال شد ❌", reply_to_message_id=message_id(msg)); return
    days = _safe_int(re.sub(r"\D+", " ", a).split()[0] if re.sub(r"\D+", " ", a).split() else DEFAULT_PLAN_DAYS, DEFAULT_PLAN_DAYS)
    exp = _activate_subscription(cid, days)
    api.safe_send_message(cid, f"اشتراک برای {days} روز فعال شد ✅\nتا: {jalali_date(exp)}", reply_to_message_id=message_id(msg))


def handle_payment(msg: dict, args: str) -> None:
    cid = chat_id(msg); a = fa_norm(args)
    if a == "card" or "کارت" in a:
        text = "💳 کارت‌به‌کارت\n\n"
        if CARD_NUMBER: text += f"شماره کارت:\n<code>{CARD_NUMBER}</code>\n"
        if CARD_HOLDER: text += f"به نام: {CARD_HOLDER}\n"
        text += f"مبلغ: {PLAN_PRICE_IRR:,} تومان\n\n{CARD_NOTE}"
        api.safe_send_message(cid, text, reply_to_message_id=message_id(msg), parse_mode="HTML", reply_markup=inline_keyboard([[btn("کپی شماره کارت", copy_text=CARD_NUMBER or "CARD_NUMBER تنظیم نشده")]]))
        return
    if PAYMENTS_ENABLED and BALE_PROVIDER_TOKEN:
        try:
            api.send_invoice(cid, "اشتراک ربات گروه", f"اشتراک {DEFAULT_PLAN_DAYS} روزه", f"sub:{cid}:{DEFAULT_PLAN_DAYS}", BALE_PROVIDER_TOKEN, PLAN_PRICE_IRR, "اشتراک")
            return
        except Exception as exc:
            api.safe_send_message(cid, f"پرداخت بله اجرا نشد 😕\n{exc}\nفعلاً کارت‌به‌کارت رو بزن.", reply_to_message_id=message_id(msg))
    handle_payment(msg, "card")


def handle_receipt(msg: dict, args: str) -> None:
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    db.conn.execute("INSERT INTO payments(group_id,user_id,method,amount,status,payload,created_at) VALUES(?,?,?,?,?,?,?)", (int(cid) if str(cid).lstrip('-').isdigit() else None, uid, "card", PLAN_PRICE_IRR, "pending", get_text(msg), now_ts()))
    api.safe_send_message(cid, "رسیدت ثبت شد ✅\nمدیر چک می‌کنه و اگه درست بود اشتراک رو فعال می‌کنه.", reply_to_message_id=message_id(msg))
    for owner in OWNER_IDS:
        api.safe_send_message(owner, f"📷 رسید جدید\nگروه: {cid}\nکاربر: {uid}\nمبلغ: {PLAN_PRICE_IRR:,}")


def handle_attack(msg: dict, args: str) -> None:
    if not require_admin(msg, "locks"): return
    cid = int(chat_id(msg)); enabled = fa_norm(args) != "off" and "خاموش" not in fa_norm(args)
    _enable_anti_raid(cid, enabled)
    api.safe_send_message(cid, "حالت ضدحمله روشن شد 🚨" if enabled else "حالت ضدحمله خاموش شد 🟢", reply_to_message_id=message_id(msg), reply_markup=_section_keyboard("attack", cid))


def handle_payment_updates(update: dict) -> bool:
    pcq = update.get("pre_checkout_query")
    if pcq:
        ok = bool(PAYMENTS_ENABLED and BALE_PROVIDER_TOKEN)
        api.answer_pre_checkout_query(pcq.get("id"), ok=ok, error_message="فعلاً پرداخت بله فعال نیست؛ کارت‌به‌کارت رو بزن.")
        return True
    msg = update.get("message") or {}
    if msg.get("successful_payment"):
        pay = msg.get("successful_payment") or {}; payload = pay.get("invoice_payload") or pay.get("payload") or ""
        cid = chat_id(msg); days = DEFAULT_PLAN_DAYS
        m = re.search(r"sub:([^:]+):(\d+)", payload)
        group_id = int(m.group(1)) if m and str(m.group(1)).lstrip('-').isdigit() else int(cid)
        if m: days = int(m.group(2))
        exp = _activate_subscription(group_id, days, "bale_wallet")
        db.log(group_id, user_id(msg_from(msg)), "successful_payment", pay)
        api.safe_send_message(cid, f"پرداخت اوکی شد ✅\nاشتراک تا {jalali_date(exp)} فعال شد.", reply_to_message_id=message_id(msg))
        return True
    return False


def handle_command(msg: dict, cmd: str, args: str) -> None:
    cid = chat_id(msg)
    if is_group(msg): db.ensure_group(int(cid), msg_chat(msg).get("title") or "")
    if cmd in {"start"}: handle_start(msg, args)
    elif cmd in {"help", "راهنما"}: api.safe_send_message(cid, help_text(), reply_to_message_id=message_id(msg), reply_markup=panel_keyboard(cid if is_group(msg) else None))
    elif cmd in {"panel", "settings", "پنل"}: handle_panel(msg, args)
    elif cmd == "id": handle_id(msg, args)
    elif cmd in {"addadmin", "deladmin", "admins"}: handle_admins(msg, cmd, args)
    elif cmd in {"grouproleadd", "grouproledel", "grouproles"} and is_group(msg): handle_grouproles(msg, cmd, args)
    elif cmd in {"lock", "unlock"} and is_group(msg): handle_lock(msg, cmd, args)
    elif cmd in {"warnsettings"} and is_group(msg): handle_warnsettings(msg, args)
    elif cmd in {"logs"} and is_group(msg): handle_logs(msg, args)
    elif cmd in {"setwelcome", "welcome", "setrules", "rules", "setgoodbye", "goodbye", "rules_toggle", "rules_accept"} and is_group(msg): handle_welcome_rules(msg, cmd, args)
    elif cmd in {"ban", "unban", "kick", "warn", "unwarn", "warnings", "mute", "unmute"} and is_group(msg): handle_moderation(msg, cmd, args)
    elif cmd == "forcejoin" and is_group(msg): handle_forcejoin(msg, args)
    elif cmd == "forcejointext" and is_group(msg): handle_forcejointext(msg, args)
    elif cmd in {"filter", "filters", "note", "notes"} and is_group(msg): handle_filter_note(msg, cmd, args)
    elif cmd in {"stats", "backup"}: handle_stats_backup(msg, cmd)
    elif cmd == "restore": handle_restore(msg, args)
    elif cmd == "premium" and is_group(msg): handle_premium(msg, args)
    elif cmd == "payment": handle_payment(msg, args)
    elif cmd == "receipt": handle_receipt(msg, args)
    elif cmd == "attack" and is_group(msg): handle_attack(msg, args)
    else: pass


def _permission_for_data(data: str) -> str:
    first = data.split(":", 1)[0]
    if first == "p":
        section = data.split(":", 1)[1] if ":" in data else "panel"
        return PERM_BY_CALLBACK.get(section, "panel")
    return PERM_BY_CALLBACK.get(first, "panel")


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query")
    if not q: return False
    qid = q.get("id") or q.get("callback_query_id"); data = q.get("data") or ""; msg = q.get("message") or {}; cid = chat_id(msg); mid = message_id(msg); actor = user_id(q.get("from") or {})
    if data == "noop":
        if qid: api.answer_callback_query(qid, "")
        return True
    if not cid:
        if qid: api.answer_callback_query(qid, "")
        return True
    try: icid = int(cid)
    except Exception: icid = cid
    if is_group(msg) and not can_manage(icid, actor, _permission_for_data(data)):
        if qid: api.answer_callback_query(qid, "این دکمه برای مدیرهاست 😅", True)
        return True
    if qid: api.answer_callback_query(qid, "")
    if data == "panel:main": _edit_or_send_panel(cid, mid, _format_main_panel(cid), panel_keyboard(cid)); return True
    if data.startswith("p:"):
        _render_section(int(cid), mid, data.split(":",1)[1]); return True
    if data.startswith("locks:"):
        category = data.split(":",1)[1] or "core"; st = _settings(int(cid)); _edit_or_send_panel(cid, mid, locks_text(st, category), locks_panel_keyboard(st, category)); return True
    if data.startswith("locktoggle:"):
        _, category, key = data.split(":",2); st = _settings(int(cid)); db.update_group_setting(int(cid), key, not bool(st.get(key))); st = _settings(int(cid)); _edit_or_send_panel(cid, mid, locks_text(st, category), locks_panel_keyboard(st, category)); return True
    if data.startswith("lockall:"):
        _, category, value = data.split(":",2); val = value == "1"; keys = ALL_LOCK_KEYS if category == "all" else [k for k,_,_ in LOCK_DEFINITIONS.get(category, [])]
        for k in keys: db.update_group_setting(int(cid), k, val)
        st = _settings(int(cid)); _edit_or_send_panel(cid, mid, locks_text(st, "core" if category == "all" else category), locks_panel_keyboard(st, "core" if category == "all" else category)); return True
    if data.startswith("warn:"):
        parts=data.split(":"); st=_settings(int(cid))
        if parts[1]=="limit": db.update_group_setting(int(cid), "warn_limit", max(1, min(20, int(st.get("warn_limit",3)) + (1 if parts[2]=="+" else -1))))
        elif parts[1]=="action": db.update_group_setting(int(cid), "warn_action", parts[2])
        elif parts[1]=="mute": db.update_group_setting(int(cid), "warn_mute_minutes", int(parts[2]))
        _render_section(int(cid), mid, "warn"); return True
    if data.startswith("logs:"):
        action=data.split(":",1)[1]
        if action=="on": db.update_group_setting(int(cid), "log_enabled", True)
        elif action=="off": db.update_group_setting(int(cid), "log_enabled", False)
        elif action=="sethere": db.update_group_setting(int(cid), "log_chat_id", int(cid)); db.update_group_setting(int(cid), "log_enabled", True)
        elif action=="test": send_group_log(int(cid), "تست لاگ", "لاگ سالمه داداش ✅")
        _render_section(int(cid), mid, "logs"); return True
    if data.startswith("stats:"):
        days=int(data.split(":",1)[1]); fake={"chat":{"id":cid,"type":"group"},"from":{"id":actor},"message_id":mid,"text":"آمار امروز" if days==1 else ("آمار ماه" if days==30 else "آمار هفته")}; handle_stats_backup(fake,"stats"); return True
    if data.startswith("force:"):
        action=data.split(":",1)[1]
        if action=="on": db.update_group_setting(int(cid), "force_join_enabled", True)
        elif action=="off": db.update_group_setting(int(cid), "force_join_enabled", False)
        elif action=="clear":
            for c in db.list_required_channels(int(cid)): db.remove_required_channel(int(cid), c["channel_id"])
        elif action=="list": api.safe_send_message(cid, _panel_text("force", int(cid)))
        _render_section(int(cid), mid, "force"); return True
    if data.startswith("welcome:") or data.startswith("goodbye:") or data.startswith("rulesaccept:"):
        part, val=data.split(":",1); key={"welcome":"welcome_enabled","goodbye":"goodbye_enabled","rulesaccept":"rules_must_accept"}[part]; db.update_group_setting(int(cid), key, val=="on"); _render_section(int(cid), mid, "welcome"); return True
    if data.startswith("rules:"):
        action=data.split(":",1)[1]
        if action in {"on","off"}: db.update_group_setting(int(cid), "rules_enabled", action=="on"); _render_section(int(cid), mid, "welcome")
        elif action=="show": api.safe_send_message(cid, "📌 قوانین گروه:\n\n" + _settings(int(cid)).get("rules_text","هنوز تنظیم نشده."), reply_markup=inline_keyboard([[btn("قبول دارم ✅", f"rulesok:{actor or 0}")]]))
        return True
    if data.startswith("rulesok:"):
        target=int(data.split(":",1)[1] or 0)
        if actor and (target==0 or actor==target): _set_rules_accepted(int(cid), actor); api.safe_send_message(cid, "مرسی، قوانین قبول شد ✅")
        return True
    if data.startswith("fjcheck:"):
        target=int(data.split(":",1)[1] or 0)
        if actor != target: return True
        fake={"chat":{"id":cid,"type":"group"},"from":{"id":actor,"first_name":"رفیق"},"message_id":mid,"text":"check"}
        if check_force_join(fake, _settings(int(cid))): api.safe_send_message(cid, "عضویتت اوکیه، خوش اومدی ✅")
        return True
    if data.startswith("backup:"):
        action=data.split(":",1)[1]
        if action=="create": handle_stats_backup({"chat":{"id":cid,"type":"group"},"from":{"id":actor},"message_id":mid}, "backup")
        elif action=="list":
            files=_recent_backups(); txt="💾 بکاپ‌های اخیر:\n" + ("\n".join(f"• {f.name}" for f in files) if files else "هنوز بکاپی نیست."); api.safe_send_message(cid, txt)
        elif action=="auto:on": db.update_group_setting(int(cid), "auto_backup_enabled", True)
        elif action=="auto:off": db.update_group_setting(int(cid), "auto_backup_enabled", False)
        _render_section(int(cid), mid, "backup"); return True
    if data.startswith("premium:"):
        action=data.split(":",1)[1]
        if action=="status": api.safe_send_message(cid, _premium_text(int(cid)))
        elif action=="add30" and is_bot_owner(actor): _activate_subscription(int(cid),30)
        elif action=="add90" and is_bot_owner(actor): _activate_subscription(int(cid),90)
        elif action=="off" and is_bot_owner(actor): _deactivate_subscription(int(cid))
        _render_section(int(cid), mid, "premium"); return True
    if data.startswith("payment:"):
        handle_payment({"chat":{"id":cid,"type":"group"},"from":{"id":actor},"message_id":mid,"text":"پرداخت"}, data.split(":",1)[1]); return True
    if data.startswith("attack:"):
        _enable_anti_raid(int(cid), data.endswith(":on")); _render_section(int(cid), mid, "attack"); return True
    if data.startswith("roles:"):
        _render_section(int(cid), mid, "roles"); return True
    return True


def process_update(update: dict) -> None:
    try:
        _maybe_auto_backup()
        if handle_payment_updates(update): return
        if handle_callback(update): return
        msg = get_message(update)
        if not msg: return
        text = get_text(msg); command = get_command(text)
        if command:
            handle_command(msg, command[0], command[1]); return
        if is_group(msg): handle_normal_group_message(msg)
    except Exception:
        log.exception("Update processing failed: %s", json.dumps(update, ensure_ascii=False)[:1500])


def polling_loop() -> None:
    log.info("Deleting webhook before polling...")
    api.delete_webhook()
    me = api.get_me()
    log.info("Bot started: %s", me)

    offset = None
    last_update_id = db.get_meta("last_update_id")
    if last_update_id and str(last_update_id).isdigit():
        offset = int(last_update_id) + 1

    # On restart, old callback/pre-checkout updates are already expired and answering
    # them only creates noisy warnings. Default: drop pending backlog once at startup.
    if CLEAR_PENDING_ON_START:
        try:
            stale = api.get_updates(offset=-1, limit=1, timeout=1)
            if stale:
                newest = stale[-1].get("update_id")
                if newest is not None:
                    offset = int(newest) + 1
                    db.set_meta("last_update_id", newest)
                    log.warning("Cleared pending updates on startup. Next offset=%s", offset)
            else:
                log.info("No pending updates to clear on startup.")
        except Exception as exc:
            log.warning("Could not clear pending updates on startup: %s", exc)

    while RUNNING:
        try:
            updates = api.get_updates(offset=offset, timeout=30)
            for upd in updates:
                update_id = upd.get("update_id")
                if update_id is not None:
                    offset = int(update_id) + 1
                    db.set_meta("last_update_id", update_id)
                process_update(upd)
        except BaleAPIError as exc:
            log.warning("Polling API error: %s", exc)
            time.sleep(3)
        except Exception:
            log.exception("Polling loop crashed")
            time.sleep(3)
    log.info("Bot stopped.")


# ============================================================
# Monetization / owner PM panel upgrade
# - Payment settings are editable from owner private chat
# - Purchases happen only in private chat
# - Groups only show subscription status + private purchase link
# ============================================================

_OLD_HANDLE_CALLBACK_MON = handle_callback
_OLD_HANDLE_COMMAND_MON = handle_command
_OLD_HANDLE_PAYMENT_UPDATES_MON = handle_payment_updates
_OLD_PROCESS_UPDATE_MON = process_update
_OLD_HANDLE_START_MON = handle_start
_OLD_HANDLE_PANEL_MON = handle_panel
_OLD_HANDLE_PREMIUM_MON = handle_premium
_OLD_HANDLE_PAYMENT_MON = handle_payment
_OLD_GET_COMMAND_MON = get_command
_OLD_POLLING_LOOP_MON = polling_loop


def ensure_monetization_schema() -> None:
    with db._lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            days INTEGER NOT NULL,
            price INTEGER NOT NULL,
            description TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 100,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            plan_id INTEGER,
            method TEXT,
            amount INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            receipt_text TEXT,
            receipt_message_id INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_states (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_orders_group_status ON orders(group_id, status);
        """)


def _json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(txt, default=None):
    try:
        return json.loads(txt or "")
    except Exception:
        return default if default is not None else {}


def cfg_get(key: str, default=None):
    row = db.conn.execute("SELECT value_json FROM bot_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return _json_loads(row["value_json"], default)


def cfg_set(key: str, value) -> None:
    with db._lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO bot_settings(key,value_json,updated_at) VALUES(?,?,?)",
            (key, _json_dumps(value), now_ts()),
        )


def cfg_bool(key: str, default: bool = False) -> bool:
    val = cfg_get(key, default)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on", "روشن"}
    return bool(val)


def cfg_int(key: str, default: int = 0) -> int:
    try:
        return int(cfg_get(key, default) or default)
    except Exception:
        return default


def seed_monetization_defaults() -> None:
    defaults = {
        "card_enabled": bool(CARD_NUMBER),
        "card_number": CARD_NUMBER,
        "card_holder": CARD_HOLDER,
        "card_note": CARD_NOTE,
        "bale_pay_enabled": bool(PAYMENTS_ENABLED and BALE_PROVIDER_TOKEN),
        "bale_provider_token": BALE_PROVIDER_TOKEN,
        "bot_username": os.getenv("BOT_USERNAME", os.getenv("BALE_BOT_USERNAME", "")).strip().lstrip("@"),
        "free_features_enabled": FREE_GROUPS_ENABLED,
    }
    for k, v in defaults.items():
        if cfg_get(k, None) is None:
            cfg_set(k, v)
    row = db.conn.execute("SELECT COUNT(*) AS c FROM plans").fetchone()
    if not row or int(row["c"] or 0) == 0:
        now = now_ts()
        plans = [
            ("💎 یک ماهه", int(DEFAULT_PLAN_DAYS or 30), int(PLAN_PRICE_IRR or 99000), "برای شروع عالیه", 1, 10),
            ("🔥 سه ماهه", 90, max(int(PLAN_PRICE_IRR or 99000) * 3 - 48000, int(PLAN_PRICE_IRR or 99000)), "به‌صرفه‌تر از ماهانه", 1, 20),
            ("🚀 شش ماهه", 180, max(int(PLAN_PRICE_IRR or 99000) * 6 - 95000, int(PLAN_PRICE_IRR or 99000)), "برای گروه‌های فعال", 1, 30),
            ("👑 یک ساله", 365, max(int(PLAN_PRICE_IRR or 99000) * 12 - 290000, int(PLAN_PRICE_IRR or 99000)), "خیالت یک سال راحته", 1, 40),
        ]
        with db._lock:
            for title, days, price, desc, active, sort_order in plans:
                db.conn.execute(
                    "INSERT INTO plans(title,days,price,description,active,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (title, days, price, desc, active, sort_order, now, now),
                )


ensure_monetization_schema()
seed_monetization_defaults()


def mask_secret(value: str, keep: int = 4) -> str:
    value = str(value or "")
    if not value:
        return "تنظیم نشده"
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * max(4, len(value) - keep) + value[-keep:]


def format_money(amount: int) -> str:
    try:
        return f"{int(amount):,}".replace(",", "٬")
    except Exception:
        return str(amount)


def get_bot_username() -> str:
    return str(cfg_get("bot_username", "") or "").strip().lstrip("@")


def set_bot_username_from_me(me: dict) -> None:
    username_ = (me or {}).get("username") or (me or {}).get("user_name") or ""
    if username_:
        cfg_set("bot_username", str(username_).lstrip("@"))


def private_buy_url(group_id: int | str) -> Optional[str]:
    uname = get_bot_username()
    if not uname:
        return None
    return f"https://ble.ir/{uname}?start=buy_{group_id}"


def set_state(uid: int, state: str, data: Optional[dict] = None) -> None:
    with db._lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO conversation_states(user_id,state,data_json,created_at) VALUES(?,?,?,?)",
            (int(uid), state, _json_dumps(data or {}), now_ts()),
        )


def get_state(uid: int) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM conversation_states WHERE user_id=?", (int(uid),)).fetchone()
    if not row:
        return None
    return {"state": row["state"], "data": _json_loads(row["data_json"], {}), "created_at": row["created_at"]}


def clear_state(uid: int) -> None:
    with db._lock:
        db.conn.execute("DELETE FROM conversation_states WHERE user_id=?", (int(uid),))


def plan_list(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM plans"
    params = []
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY sort_order ASC, id ASC"
    return [dict(r) for r in db.conn.execute(q, params).fetchall()]


def plan_get(plan_id: int) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM plans WHERE id=?", (int(plan_id),)).fetchone()
    return dict(row) if row else None


def plan_add(title: str, days: int, price: int, description: str = "") -> int:
    with db._lock:
        cur = db.conn.execute(
            "INSERT INTO plans(title,days,price,description,active,sort_order,created_at,updated_at) VALUES(?,?,?,?,1,100,?,?)",
            (title.strip(), int(days), int(price), description.strip(), now_ts(), now_ts()),
        )
        return int(cur.lastrowid)


def plan_toggle(plan_id: int) -> None:
    with db._lock:
        row = plan_get(plan_id)
        if row:
            db.conn.execute("UPDATE plans SET active=?, updated_at=? WHERE id=?", (0 if row.get("active") else 1, now_ts(), int(plan_id)))


def plan_delete(plan_id: int) -> bool:
    with db._lock:
        cur = db.conn.execute("DELETE FROM plans WHERE id=?", (int(plan_id),))
        return cur.rowcount > 0


def create_order(uid: int, group_id: int, plan_id: int, method: Optional[str] = None, status: str = "draft") -> int:
    plan = plan_get(plan_id)
    if not plan:
        raise ValueError("plan not found")
    with db._lock:
        cur = db.conn.execute(
            "INSERT INTO orders(user_id,group_id,plan_id,method,amount,status,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(uid), int(group_id), int(plan_id), method, int(plan["price"]), status, _json_dumps({}), now_ts(), now_ts()),
        )
        return int(cur.lastrowid)


def order_get(order_id: int) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM orders WHERE id=?", (int(order_id),)).fetchone()
    return dict(row) if row else None


def order_update(order_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now_ts()
    cols = ",".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [int(order_id)]
    with db._lock:
        db.conn.execute(f"UPDATE orders SET {cols} WHERE id=?", vals)


def owner_panel_text() -> str:
    pending = db.conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status='receipt_pending'").fetchone()["c"]
    active_groups = db.conn.execute("SELECT COUNT(*) AS c FROM subscriptions WHERE active=1 AND (expires_at IS NULL OR expires_at>?)", (now_ts(),)).fetchone()["c"]
    sales = db.conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM orders WHERE status IN ('paid','approved')").fetchone()["s"]
    return (
        "👑 پنل خودت\n\n"
        "اینجا تنظیمات فروش و اشتراک‌هاست.\n"
        f"🧾 رسیدهای منتظر: {pending}\n"
        f"💎 گروه‌های فعال: {active_groups}\n"
        f"💰 فروش ثبت‌شده: {format_money(sales)} تومان\n\n"
        "هر بخش رو با دکمه‌ها مدیریت کن 👇"
    )


def owner_main_keyboard() -> dict:
    return inline_keyboard([
        [btn("💳 تنظیمات پرداخت", "owner:payments"), btn("💎 پلن‌ها", "owner:plans")],
        [btn("🧾 رسیدها", "owner:receipts"), btn("📦 سفارش‌ها", "owner:orders")],
        [btn("👥 گروه‌های فعال", "owner:groups"), btn("📊 آمار فروش", "owner:sales")],
        [btn("⚙️ تنظیمات کلی", "owner:general")],
    ])


def payment_settings_text() -> str:
    return (
        "💳 تنظیمات پرداخت\n\n"
        f"کارت‌به‌کارت: {'روشن ✅' if cfg_bool('card_enabled') else 'خاموش ❌'}\n"
        f"شماره کارت: <code>{mask_secret(cfg_get('card_number',''), 4)}</code>\n"
        f"صاحب کارت: {cfg_get('card_holder','تنظیم نشده') or 'تنظیم نشده'}\n\n"
        f"پرداخت بله: {'روشن ✅' if cfg_bool('bale_pay_enabled') else 'خاموش ❌'}\n"
        f"توکن/مرچنت بله: <code>{mask_secret(cfg_get('bale_provider_token',''), 6)}</code>\n\n"
        "برای تغییر هرکدوم، دکمه‌اش رو بزن."
    )


def payment_settings_keyboard() -> dict:
    return inline_keyboard([
        [btn("💳 تنظیم شماره کارت", "payset:card_number"), btn("👤 نام صاحب کارت", "payset:card_holder")],
        [btn("📝 متن راهنمای کارت", "payset:card_note")],
        [btn("🔑 تنظیم توکن بله", "payset:bale_token")],
        [btn("✅/❌ کارت‌به‌کارت", "payset:toggle_card"), btn("✅/❌ پرداخت بله", "payset:toggle_bale")],
        [btn("🧪 تست پرداخت بله", "payset:test_bale")],
        [btn("⬅️ برگشت", "owner:main")],
    ])


def plans_text() -> str:
    plans = plan_list(False)
    lines = ["💎 پلن‌های اشتراک", ""]
    if not plans:
        lines.append("فعلاً پلنی نداری.")
    for p in plans:
        lines.append(f"#{p['id']} {'✅' if p['active'] else '❌'} {p['title']} — {p['days']} روز — {format_money(p['price'])} تومان")
    lines.append("\nبرای پلن جدید، دکمه افزودن رو بزن و فرمت خواسته‌شده رو بفرست.")
    return "\n".join(lines)


def plans_keyboard() -> dict:
    rows = [[btn("➕ پلن جدید", "plan:add")]]
    for p in plan_list(False)[:12]:
        rows.append([btn(f"{'خاموش' if p['active'] else 'روشن'} #{p['id']}", f"plan:toggle:{p['id']}"), btn(f"🗑 حذف #{p['id']}", f"plan:delete:{p['id']}")])
    rows.append([btn("⬅️ برگشت", "owner:main")])
    return inline_keyboard(rows)


def sales_text() -> str:
    today = time.strftime("%Y%m%d")
    paid_total = db.conn.execute("SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM orders WHERE status IN ('paid','approved')").fetchone()
    pending = db.conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status='receipt_pending'").fetchone()["c"]
    orders_today = db.conn.execute("SELECT COUNT(*) AS c FROM orders WHERE created_at>=?", (now_ts()-86400,)).fetchone()["c"]
    return (
        "📊 آمار فروش\n\n"
        f"فروش کل: {format_money(paid_total['s'])} تومان\n"
        f"سفارش موفق: {paid_total['c']}\n"
        f"سفارش امروز: {orders_today}\n"
        f"رسید منتظر بررسی: {pending}\n"
    )


def pending_receipts_text() -> str:
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM orders WHERE status='receipt_pending' ORDER BY created_at DESC LIMIT 10").fetchall()]
    lines = ["🧾 رسیدهای منتظر بررسی", ""]
    if not rows:
        lines.append("فعلاً رسیدی تو صف نیست ✅")
    for r in rows:
        plan = plan_get(r.get("plan_id") or 0) or {}
        lines.append(f"#{r['id']} — {plan.get('title','پلن')} — {format_money(r['amount'])} تومان — گروه {r['group_id']} — کاربر {r['user_id']}")
    return "\n".join(lines)


def pending_receipts_keyboard() -> dict:
    rows = []
    for r in [dict(x) for x in db.conn.execute("SELECT id FROM orders WHERE status='receipt_pending' ORDER BY created_at DESC LIMIT 8").fetchall()]:
        oid = r["id"]
        rows.append([btn(f"✅ تایید #{oid}", f"order:approve:{oid}"), btn(f"❌ رد #{oid}", f"order:reject:{oid}")])
    rows.append([btn("⬅️ برگشت", "owner:main")])
    return inline_keyboard(rows)


def groups_text() -> str:
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM subscriptions WHERE active=1 AND (expires_at IS NULL OR expires_at>?) ORDER BY expires_at ASC LIMIT 20", (now_ts(),)).fetchall()]
    lines = ["👥 گروه‌های فعال", ""]
    if not rows:
        lines.append("فعلاً گروه فعال نداری.")
    for r in rows:
        lines.append(f"• {r['group_id']} — {r.get('plan') or 'premium'} — تا {jalali_date(int(r['expires_at'])) if r.get('expires_at') else 'نامحدود'}")
    return "\n".join(lines)


def owner_general_text() -> str:
    return (
        "⚙️ تنظیمات کلی\n\n"
        f"یوزرنیم ربات: @{get_bot_username() or 'تنظیم نشده'}\n"
        "برای اینکه دکمه خرید از گروه مستقیم پیوی رو باز کنه، یوزرنیم ربات باید تنظیم باشه."
    )


def owner_general_keyboard() -> dict:
    return inline_keyboard([
        [btn("🤖 تنظیم یوزرنیم ربات", "owner:set_username")],
        [btn("⬅️ برگشت", "owner:main")],
    ])


def render_owner_section(chat_id_: int, message_id_: Optional[int], section: str) -> None:
    if section == "main":
        _edit_or_send_panel(chat_id_, message_id_, owner_panel_text(), owner_main_keyboard()); return
    if section == "payments":
        _edit_or_send_panel(chat_id_, message_id_, payment_settings_text(), payment_settings_keyboard()); return
    if section == "plans":
        _edit_or_send_panel(chat_id_, message_id_, plans_text(), plans_keyboard()); return
    if section == "receipts":
        _edit_or_send_panel(chat_id_, message_id_, pending_receipts_text(), pending_receipts_keyboard()); return
    if section == "orders":
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 12").fetchall()]
        lines = ["📦 سفارش‌های اخیر", ""]
        if not rows: lines.append("هنوز سفارشی ثبت نشده.")
        for r in rows:
            lines.append(f"#{r['id']} — {r['status']} — {format_money(r['amount'])} تومان — گروه {r['group_id']} — کاربر {r['user_id']}")
        _edit_or_send_panel(chat_id_, message_id_, "\n".join(lines), inline_keyboard([[btn("⬅️ برگشت", "owner:main")]])); return
    if section == "groups":
        _edit_or_send_panel(chat_id_, message_id_, groups_text(), inline_keyboard([[btn("⬅️ برگشت", "owner:main")]])); return
    if section == "sales":
        _edit_or_send_panel(chat_id_, message_id_, sales_text(), inline_keyboard([[btn("⬅️ برگشت", "owner:main")]])); return
    if section == "general":
        _edit_or_send_panel(chat_id_, message_id_, owner_general_text(), owner_general_keyboard()); return


def group_subscription_text(cid: int) -> str:
    sub = _subscription_status(int(cid))
    title = db.get_group_title(int(cid))
    if sub.get("active"):
        if sub.get("plan") == "free":
            status = "نسخه رایگان فعاله ✅\nبرای باز شدن همه امکانات، اشتراک پرمیوم بگیر."
        elif sub.get("expires_at"):
            status = f"پرمیوم فعاله ✅\n⏳ اعتبار: تا {jalali_date(int(sub['expires_at']))}"
        else:
            status = "پرمیوم فعاله ✅\n⏳ اعتبار: نامحدود"
    else:
        status = "فعال نیست ❌"
    plans = plan_list(True)
    plan_lines = "\n".join([f"• {p['title']} — {p['days']} روز — {format_money(p['price'])} تومان" for p in plans[:6]]) or "فعلاً پلنی تعریف نشده."
    return (
        f"💎 اشتراک گروه\n\n"
        f"📌 گروه: {title}\n"
        f"وضعیت: {status}\n\n"
        "پلن‌ها:\n"
        f"{plan_lines}\n\n"
        "خرید و تمدید فقط داخل پیوی ربات انجام میشه؛ اینجا فقط وضعیت و پلن‌ها رو می‌بینی."
    )


def group_subscription_keyboard(cid: int) -> dict:
    url = private_buy_url(cid)
    rows = []
    if url:
        rows.append([btn("🛒 خرید/تمدید در پیوی", url=url)])
    rows.append([btn("📋 کپی دستور خرید", copy_text=f"خرید اشتراک {cid}")])
    rows.append([btn("⬅️ برگشت", "panel:main")])
    return inline_keyboard(rows)


def private_store_text(group_id: Optional[int] = None) -> str:
    plans = plan_list(True)
    lines = ["🛒 فروشگاه اشتراک", ""]
    if group_id:
        lines.append(f"برای گروه: <code>{group_id}</code>")
        lines.append("")
    lines.append("کدوم پلن رو می‌خوای؟")
    if not plans:
        lines.append("\nفعلاً پلنی فعال نیست. بعداً دوباره سر بزن.")
    return "\n".join(lines)


def private_store_keyboard(group_id: Optional[int] = None) -> dict:
    rows = []
    gid = int(group_id or 0)
    for p in plan_list(True):
        rows.append([btn(f"{p['title']} - {format_money(p['price'])} تومان", f"buy:select:{gid}:{p['id']}")])
    rows.append([btn("⬅️ منوی اصلی", "pm:main")])
    return inline_keyboard(rows)


def private_main_text(user: dict) -> str:
    return (
        f"سلام {first_name(user)} جان 🌹\n\n"
        "اینجا می‌تونی برای گروهت اشتراک بخری یا تمدید کنی.\n"
        "اگه از داخل گروه اومده باشی، خودم می‌فهمم خرید برای کدوم گروهه."
    )


def private_main_keyboard(uid: Optional[int] = None) -> dict:
    rows = [[btn("🛒 خرید اشتراک", "buy:start:0")], [btn("📦 سفارش‌های من", "pm:myorders")]]
    if is_bot_owner(uid):
        rows.insert(0, [btn("👑 پنل مالک", "owner:main")])
    return inline_keyboard(rows)


def ask_group_id_text() -> str:
    return (
        "برای خرید باید بدونم اشتراک برای کدوم گروهه.\n\n"
        "راحت‌ترین راه: داخل همون گروه بزن «اشتراک» و بعد دکمه خرید رو بزن.\n"
        "یا آیدی عددی گروه رو همینجا بفرست."
    )


def order_confirm_text(order_id: int) -> str:
    order = order_get(order_id)
    if not order:
        return "این سفارش پیدا نشد 😕"
    plan = plan_get(order["plan_id"] or 0) or {}
    return (
        "🧾 پیش‌فاکتور\n\n"
        f"شماره سفارش: #{order_id}\n"
        f"گروه: <code>{order['group_id']}</code>\n"
        f"پلن: {plan.get('title','پلن')}\n"
        f"مدت: {plan.get('days','?')} روز\n"
        f"مبلغ: {format_money(order['amount'])} تومان\n\n"
        "روش پرداخت رو انتخاب کن 👇"
    )


def order_confirm_keyboard(order_id: int) -> dict:
    rows = []
    if cfg_bool("bale_pay_enabled") and cfg_get("bale_provider_token", ""):
        rows.append([btn("🟢 پرداخت با بله", f"buy:paybale:{order_id}")])
    if cfg_bool("card_enabled") and cfg_get("card_number", ""):
        rows.append([btn("💳 کارت‌به‌کارت", f"buy:card:{order_id}")])
    if not rows:
        rows.append([btn("هیچ پرداختی روشن نیست 😕", "noop")])
    rows.append([btn("⬅️ برگشت به پلن‌ها", "buy:start:0")])
    return inline_keyboard(rows)


def my_orders_text(uid: int) -> str:
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (int(uid),)).fetchall()]
    lines = ["📦 سفارش‌های من", ""]
    if not rows:
        lines.append("هنوز سفارشی نداری.")
    for r in rows:
        plan = plan_get(r.get("plan_id") or 0) or {}
        lines.append(f"#{r['id']} — {plan.get('title','پلن')} — {r['status']} — {format_money(r['amount'])} تومان")
    return "\n".join(lines)


def _premium_text(cid: int) -> str:  # override: groups only see status
    return group_subscription_text(int(cid))


def handle_start(msg: dict, args: str) -> None:  # override
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if is_private(msg):
        a = (args or "").strip()
        m = re.search(r"buy_(-?\d+)", a)
        if m:
            gid = int(m.group(1))
            api.safe_send_message(cid, private_store_text(gid), parse_mode="HTML", reply_markup=private_store_keyboard(gid))
            return
        api.safe_send_message(cid, private_main_text(msg_from(msg)), reply_markup=private_main_keyboard(uid))
        return
    api.safe_send_message(cid, "سلام 🌹\nبرای مدیریت گروه بزن: پنل\nبرای دیدن اشتراک بزن: اشتراک", reply_to_message_id=message_id(msg))


def handle_panel(msg: dict, args: str) -> None:  # override
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if is_private(msg):
        if is_bot_owner(uid):
            api.safe_send_message(cid, owner_panel_text(), reply_markup=owner_main_keyboard())
        else:
            api.safe_send_message(cid, private_main_text(msg_from(msg)), reply_markup=private_main_keyboard(uid))
        return
    if is_group(msg) and not require_admin(msg, "panel"):
        return
    api.safe_send_message(cid, _format_main_panel(cid), reply_markup=panel_keyboard(cid))


def handle_premium(msg: dict, args: str) -> None:  # override
    cid = chat_id(msg)
    if is_group(msg):
        if not require_admin(msg, "premium"):
            return
        api.safe_send_message(cid, group_subscription_text(int(cid)), reply_to_message_id=message_id(msg), parse_mode="HTML", reply_markup=group_subscription_keyboard(int(cid)))
        return
    # private: open purchase flow
    a = fa_norm(args or "")
    nums = re.findall(r"-?\d+", a)
    gid = int(nums[0]) if nums else 0
    if gid:
        api.safe_send_message(cid, private_store_text(gid), parse_mode="HTML", reply_markup=private_store_keyboard(gid))
    else:
        api.safe_send_message(cid, ask_group_id_text(), reply_markup=private_main_keyboard(user_id(msg_from(msg))))


def handle_payment(msg: dict, args: str) -> None:  # override
    cid = chat_id(msg)
    if is_group(msg):
        handle_premium(msg, "")
        return
    handle_premium(msg, args)


def handle_receipt(msg: dict, args: str) -> None:  # override: only private receipt workflow
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if is_group(msg):
        api.safe_send_message(cid, "رسید رو لطفاً داخل پیوی ربات بفرست؛ اینجا شلوغ نشه 😅", reply_to_message_id=message_id(msg))
        return
    st = get_state(uid) if uid else None
    if not st or st.get("state") != "awaiting_receipt":
        api.safe_send_message(cid, "اول یک پلن رو انتخاب کن و کارت‌به‌کارت رو بزن، بعد رسید رو همینجا بفرست.", reply_markup=private_main_keyboard(uid))
        return
    process_private_state(msg, st)


def process_private_state(msg: dict, state_row: dict) -> bool:
    uid = user_id(msg_from(msg)); cid = chat_id(msg)
    if not uid:
        return False
    state = state_row.get("state")
    data = state_row.get("data") or {}
    text = get_text(msg).strip()
    if fa_norm(text) in {"لغو", "انصراف", "cancel"}:
        clear_state(uid)
        api.safe_send_message(cid, "لغو شد ✅", reply_markup=private_main_keyboard(uid))
        return True
    if state == "set_card_number":
        cleaned = re.sub(r"\D+", "", text)
        if len(cleaned) < 12:
            api.safe_send_message(cid, "شماره کارت کوتاهه؛ دوباره درست بفرست.")
            return True
        cfg_set("card_number", cleaned); cfg_set("card_enabled", True); clear_state(uid)
        api.safe_send_message(cid, "شماره کارت ذخیره شد ✅", reply_markup=payment_settings_keyboard())
        return True
    if state == "set_card_holder":
        cfg_set("card_holder", text[:80]); clear_state(uid)
        api.safe_send_message(cid, "نام صاحب کارت ذخیره شد ✅", reply_markup=payment_settings_keyboard())
        return True
    if state == "set_card_note":
        cfg_set("card_note", text[:1000]); clear_state(uid)
        api.safe_send_message(cid, "متن راهنمای کارت ذخیره شد ✅", reply_markup=payment_settings_keyboard())
        return True
    if state == "set_bale_token":
        cfg_set("bale_provider_token", text.strip()); cfg_set("bale_pay_enabled", True); clear_state(uid)
        api.safe_send_message(cid, "توکن/مرچنت بله ذخیره شد ✅\nبرای امنیت، کامل نمایش داده نمیشه.", reply_markup=payment_settings_keyboard())
        return True
    if state == "set_bot_username":
        cfg_set("bot_username", text.strip().lstrip("@")); clear_state(uid)
        api.safe_send_message(cid, "یوزرنیم ربات ذخیره شد ✅", reply_markup=owner_general_keyboard())
        return True
    if state == "add_plan":
        parts = [x.strip() for x in re.split(r"\|", text)]
        if len(parts) < 3:
            api.safe_send_message(cid, "فرمتش اینه:\nنام پلن | تعداد روز | قیمت | توضیح اختیاری")
            return True
        title = parts[0]; days = _safe_int(parts[1], 0); price = _safe_int(parts[2].replace(",", ""), 0); desc = parts[3] if len(parts) > 3 else ""
        if not title or days <= 0 or price <= 0:
            api.safe_send_message(cid, "نام، روز یا قیمت درست نیست؛ دوباره بفرست.")
            return True
        pid = plan_add(title, days, price, desc); clear_state(uid)
        api.safe_send_message(cid, f"پلن #{pid} اضافه شد ✅", reply_markup=plans_keyboard())
        return True
    if state == "await_group_id":
        nums = re.findall(r"-?\d+", text)
        if not nums:
            api.safe_send_message(cid, "آیدی عددی گروه رو بفرست؛ یا از داخل گروه دکمه خرید رو بزن.")
            return True
        gid = int(nums[0]); clear_state(uid)
        api.safe_send_message(cid, private_store_text(gid), parse_mode="HTML", reply_markup=private_store_keyboard(gid))
        return True
    if state == "awaiting_receipt":
        oid = _safe_int(data.get("order_id"), 0)
        order = order_get(oid)
        if not order or int(order["user_id"]) != int(uid):
            clear_state(uid)
            api.safe_send_message(cid, "این سفارش پیدا نشد؛ دوباره از اول خرید رو بزن.")
            return True
        receipt_text = text or "[رسید بدون متن / احتمالاً عکس یا فایل]"
        order_update(oid, status="receipt_pending", method="card", receipt_text=receipt_text[:1000], receipt_message_id=message_id(msg) or 0)
        clear_state(uid)
        api.safe_send_message(cid, "رسیدت ثبت شد ✅\nیه کم صبر کن تا بررسیش کنم.", reply_markup=private_main_keyboard(uid))
        notify_owners_receipt(oid)
        return True
    return False


def notify_owners_receipt(order_id: int) -> None:
    order = order_get(order_id)
    if not order:
        return
    plan = plan_get(order.get("plan_id") or 0) or {}
    text = (
        "🧾 رسید جدید اومد\n\n"
        f"سفارش: #{order_id}\n"
        f"کاربر: <code>{order['user_id']}</code>\n"
        f"گروه: <code>{order['group_id']}</code>\n"
        f"پلن: {plan.get('title','پلن')}\n"
        f"مبلغ: {format_money(order['amount'])} تومان\n\n"
        f"متن رسید:\n{order.get('receipt_text') or '-'}\n\n"
        "تاییدش کنم؟"
    )
    kb = inline_keyboard([[btn("✅ تایید و فعال‌سازی", f"order:approve:{order_id}"), btn("❌ رد رسید", f"order:reject:{order_id}")]])
    for owner in OWNER_IDS:
        api.safe_send_message(owner, text, parse_mode="HTML", reply_markup=kb)


def approve_order(order_id: int, actor: Optional[int] = None) -> str:
    order = order_get(order_id)
    if not order:
        return "سفارش پیدا نشد 😕"
    plan = plan_get(order.get("plan_id") or 0)
    if not plan:
        return "پلن این سفارش پیدا نشد 😕"
    exp = _activate_subscription(int(order["group_id"]), int(plan["days"]), plan.get("title") or "premium")
    order_update(order_id, status="approved", payload_json=_json_dumps({"approved_by": actor, "expires_at": exp}))
    try:
        api.safe_send_message(order["user_id"], f"پرداختت تایید شد ✅\nاشتراک گروه تا {jalali_date(exp)} فعال شد.")
    except Exception:
        pass
    try:
        api.safe_send_message(order["group_id"], f"💎 اشتراک گروه فعال شد ✅\nتا: {jalali_date(exp)}")
    except Exception:
        pass
    return f"سفارش #{order_id} تایید شد ✅"


def reject_order(order_id: int, actor: Optional[int] = None) -> str:
    order = order_get(order_id)
    if not order:
        return "سفارش پیدا نشد 😕"
    order_update(order_id, status="rejected", payload_json=_json_dumps({"rejected_by": actor}))
    try:
        api.safe_send_message(order["user_id"], f"رسید سفارش #{order_id} رد شد ❌\nاگه اشتباهی شده، دوباره رسید درست رو بفرست.")
    except Exception:
        pass
    return f"سفارش #{order_id} رد شد ❌"


def send_card_instructions(cid: int, order_id: int) -> None:
    order = order_get(order_id); plan = plan_get((order or {}).get("plan_id") or 0) if order else None
    if not order or not plan:
        api.safe_send_message(cid, "سفارش پیدا نشد 😕")
        return
    card = cfg_get("card_number", "") or "تنظیم نشده"
    holder = cfg_get("card_holder", "") or "تنظیم نشده"
    note = cfg_get("card_note", CARD_NOTE) or CARD_NOTE
    text = (
        "💳 کارت‌به‌کارت\n\n"
        f"سفارش: #{order_id}\n"
        f"پلن: {plan['title']}\n"
        f"مبلغ: {format_money(order['amount'])} تومان\n\n"
        f"شماره کارت:\n<code>{card}</code>\n"
        f"به نام: {holder}\n\n"
        f"{note}\n\n"
        "بعد از واریز، عکس یا متن رسید رو همینجا بفرست."
    )
    api.safe_send_message(cid, text, parse_mode="HTML", reply_markup=inline_keyboard([[btn("📋 کپی شماره کارت", copy_text=card)], [btn("لغو", "pm:main")]]))
    set_state(int(order["user_id"]), "awaiting_receipt", {"order_id": order_id})
    order_update(order_id, method="card", status="waiting_receipt")


def send_bale_invoice_for_order(cid: int, order_id: int) -> None:
    order = order_get(order_id); plan = plan_get((order or {}).get("plan_id") or 0) if order else None
    provider = cfg_get("bale_provider_token", "") or ""
    if not order or not plan:
        api.safe_send_message(cid, "سفارش پیدا نشد 😕")
        return
    if not cfg_bool("bale_pay_enabled") or not provider:
        api.safe_send_message(cid, "پرداخت بله فعلاً خاموشه؛ کارت‌به‌کارت رو بزن.")
        return
    payload = f"order:{order_id}"
    try:
        api.send_invoice(cid, "اشتراک ربات گروه", f"{plan['title']} برای گروه {order['group_id']}", payload, provider, int(order["amount"]), "اشتراک")
        order_update(order_id, method="bale", status="invoice_sent")
    except Exception as exc:
        api.safe_send_message(cid, f"فاکتور بله ساخته نشد 😕\n{exc}\nفعلاً کارت‌به‌کارت رو بزن.")


def handle_owner_callback(q: dict, data: str, cid, mid, actor: Optional[int]) -> bool:
    if not is_bot_owner(actor):
        api.answer_callback_query(q.get("id") or q.get("callback_query_id"), "این قسمت فقط برای مالک رباته 😅", True)
        return True
    qid = q.get("id") or q.get("callback_query_id")
    if qid: api.answer_callback_query(qid, "")
    if data.startswith("owner:"):
        action = data.split(":", 1)[1]
        if action == "set_username":
            set_state(actor, "set_bot_username", {})
            api.safe_send_message(cid, "یوزرنیم ربات رو بدون @ بفرست. مثال: my_bot")
            return True
        render_owner_section(cid, mid, action)
        return True
    if data.startswith("payset:"):
        action = data.split(":", 1)[1]
        if action == "card_number": set_state(actor, "set_card_number", {}); api.safe_send_message(cid, "شماره کارت جدید رو بفرست:")
        elif action == "card_holder": set_state(actor, "set_card_holder", {}); api.safe_send_message(cid, "نام صاحب کارت رو بفرست:")
        elif action == "card_note": set_state(actor, "set_card_note", {}); api.safe_send_message(cid, "متن راهنمای پرداخت کارت‌به‌کارت رو بفرست:")
        elif action == "bale_token": set_state(actor, "set_bale_token", {}); api.safe_send_message(cid, "توکن/مرچنت پرداخت بله رو بفرست:")
        elif action == "toggle_card": cfg_set("card_enabled", not cfg_bool("card_enabled")); render_owner_section(cid, mid, "payments")
        elif action == "toggle_bale": cfg_set("bale_pay_enabled", not cfg_bool("bale_pay_enabled")); render_owner_section(cid, mid, "payments")
        elif action == "test_bale": api.safe_send_message(cid, "برای تست، از فروشگاه یک پلن انتخاب کن. اگه توکن درست باشه فاکتور ساخته میشه ✅")
        return True
    if data.startswith("plan:"):
        parts = data.split(":")
        action = parts[1]
        if action == "add":
            set_state(actor, "add_plan", {})
            api.safe_send_message(cid, "پلن جدید رو اینطوری بفرست:\nنام پلن | تعداد روز | قیمت | توضیح اختیاری\n\nمثال:\nپلن تست | 7 | 29000 | برای تست یک هفته‌ای")
        elif action == "toggle" and len(parts) > 2:
            plan_toggle(int(parts[2])); render_owner_section(cid, mid, "plans")
        elif action == "delete" and len(parts) > 2:
            plan_delete(int(parts[2])); render_owner_section(cid, mid, "plans")
        return True
    return False


def handle_buy_callback(q: dict, data: str, cid, mid, actor: Optional[int]) -> bool:
    qid = q.get("id") or q.get("callback_query_id")
    if qid: api.answer_callback_query(qid, "")
    if data == "pm:main":
        clear_state(actor or 0)
        _edit_or_send_panel(cid, mid, private_main_text(q.get("from") or {}), private_main_keyboard(actor))
        return True
    if data == "pm:myorders":
        _edit_or_send_panel(cid, mid, my_orders_text(actor or 0), inline_keyboard([[btn("⬅️ برگشت", "pm:main")]]))
        return True
    if data.startswith("buy:start:"):
        gid = _safe_int(data.split(":", 2)[2], 0)
        if not gid:
            set_state(actor or 0, "await_group_id", {})
            api.safe_send_message(cid, ask_group_id_text())
            return True
        _edit_or_send_panel(cid, mid, private_store_text(gid), private_store_keyboard(gid))
        return True
    if data.startswith("buy:select:"):
        _, _, gid_s, pid_s = data.split(":", 3)
        gid = _safe_int(gid_s, 0); pid = _safe_int(pid_s, 0)
        if not gid:
            set_state(actor or 0, "await_group_id", {"plan_id": pid})
            api.safe_send_message(cid, ask_group_id_text())
            return True
        oid = create_order(actor or 0, gid, pid)
        _edit_or_send_panel(cid, mid, order_confirm_text(oid), order_confirm_keyboard(oid))
        return True
    if data.startswith("buy:paybale:"):
        oid = _safe_int(data.split(":", 2)[2], 0)
        send_bale_invoice_for_order(cid, oid)
        return True
    if data.startswith("buy:card:"):
        oid = _safe_int(data.split(":", 2)[2], 0)
        send_card_instructions(cid, oid)
        return True
    return False


def handle_order_callback(q: dict, data: str, cid, mid, actor: Optional[int]) -> bool:
    qid = q.get("id") or q.get("callback_query_id")
    if not is_bot_owner(actor):
        if qid: api.answer_callback_query(qid, "این دکمه برای مالک رباته 😅", True)
        return True
    if qid: api.answer_callback_query(qid, "")
    parts = data.split(":")
    if len(parts) < 3:
        return True
    oid = _safe_int(parts[2], 0)
    if parts[1] == "approve":
        txt = approve_order(oid, actor)
    else:
        txt = reject_order(oid, actor)
    _edit_or_send_panel(cid, mid, txt + "\n\n" + pending_receipts_text(), pending_receipts_keyboard())
    return True


def handle_callback(update: dict) -> bool:  # override
    q = update.get("callback_query")
    if not q:
        return False
    data = q.get("data") or ""
    msg = q.get("message") or {}; cid = chat_id(msg); mid = message_id(msg); actor = user_id(q.get("from") or {})
    if data.startswith("owner:") or data.startswith("payset:") or data.startswith("plan:"):
        return handle_owner_callback(q, data, cid, mid, actor)
    if data.startswith("buy:") or data.startswith("pm:"):
        return handle_buy_callback(q, data, cid, mid, actor)
    if data.startswith("order:"):
        return handle_order_callback(q, data, cid, mid, actor)
    if data.startswith("payment:") and is_group(msg):
        qid = q.get("id") or q.get("callback_query_id")
        if qid: api.answer_callback_query(qid, "خرید از پیوی انجام میشه 👇", True)
        api.safe_send_message(cid, group_subscription_text(int(cid)), parse_mode="HTML", reply_markup=group_subscription_keyboard(int(cid)))
        return True
    if data.startswith("premium:") and is_group(msg):
        qid = q.get("id") or q.get("callback_query_id")
        if qid: api.answer_callback_query(qid, "")
        _edit_or_send_panel(cid, mid, group_subscription_text(int(cid)), group_subscription_keyboard(int(cid)))
        return True
    return _OLD_HANDLE_CALLBACK_MON(update)


def handle_payment_updates(update: dict) -> bool:  # override dynamic DB token/order payloads
    pcq = update.get("pre_checkout_query")
    if pcq:
        ok = bool(cfg_bool("bale_pay_enabled") and cfg_get("bale_provider_token", ""))
        api.answer_pre_checkout_query(pcq.get("id"), ok=ok, error_message="فعلاً پرداخت بله فعال نیست؛ کارت‌به‌کارت رو بزن.")
        return True
    msg = update.get("message") or {}
    if msg.get("successful_payment"):
        pay = msg.get("successful_payment") or {}; payload = pay.get("invoice_payload") or pay.get("payload") or ""
        uid = user_id(msg_from(msg)); cid = chat_id(msg)
        m_order = re.search(r"order:(\d+)", payload)
        if m_order:
            oid = int(m_order.group(1)); order = order_get(oid); plan = plan_get((order or {}).get("plan_id") or 0) if order else None
            if order and plan:
                exp = _activate_subscription(int(order["group_id"]), int(plan["days"]), plan.get("title") or "bale_wallet")
                order_update(oid, status="paid", method="bale", payload_json=_json_dumps({"payment": pay, "expires_at": exp}))
                api.safe_send_message(cid, f"پرداخت اوکی شد ✅\nاشتراک گروه تا {jalali_date(exp)} فعال شد.")
                try: api.safe_send_message(order["group_id"], f"💎 اشتراک گروه فعال شد ✅\nتا: {jalali_date(exp)}")
                except Exception: pass
                return True
        return _OLD_HANDLE_PAYMENT_UPDATES_MON(update)
    return False


def get_command(text: str) -> Optional[Tuple[str, str]]:  # override command aliases for PM monetization
    base = _OLD_GET_COMMAND_MON(text)
    if base:
        return base
    t = fa_norm(text or "")
    if not t:
        return None
    if t in {"پنل مالک", "پنل فروش", "مدیریت فروش"}:
        return ("ownerpanel", "")
    if t.startswith("خرید اشتراک") or t == "خرید" or t == "اشتراک":
        args = t.replace("خرید اشتراک", "", 1).replace("خرید", "", 1).replace("اشتراک", "", 1).strip()
        return ("buy", args)
    return None


def handle_command(msg: dict, cmd: str, args: str) -> None:  # override PM/owner commands first
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if cmd == "ownerpanel" and is_private(msg):
        if is_bot_owner(uid):
            api.safe_send_message(cid, owner_panel_text(), reply_markup=owner_main_keyboard())
        else:
            api.safe_send_message(cid, "این بخش برای مالک رباته 😅")
        return
    if cmd == "buy" and is_private(msg):
        nums = re.findall(r"-?\d+", args or "")
        gid = int(nums[0]) if nums else 0
        if gid:
            api.safe_send_message(cid, private_store_text(gid), parse_mode="HTML", reply_markup=private_store_keyboard(gid))
        else:
            set_state(uid or 0, "await_group_id", {})
            api.safe_send_message(cid, ask_group_id_text(), reply_markup=private_main_keyboard(uid))
        return
    return _OLD_HANDLE_COMMAND_MON(msg, cmd, args)


def process_update(update: dict) -> None:  # override to support PM state before normal commands
    try:
        _maybe_auto_backup()
        if handle_payment_updates(update): return
        if handle_callback(update): return
        msg = get_message(update)
        if not msg: return
        if is_private(msg):
            uid = user_id(msg_from(msg)); st = get_state(uid) if uid else None
            if st and process_private_state(msg, st):
                return
        text = get_text(msg); command = get_command(text)
        if command:
            handle_command(msg, command[0], command[1]); return
        if is_group(msg):
            handle_normal_group_message(msg)
        elif is_private(msg) and text:
            api.safe_send_message(chat_id(msg), private_main_text(msg_from(msg)), reply_markup=private_main_keyboard(user_id(msg_from(msg))))
    except Exception:
        log.exception("Update processing failed: %s", json.dumps(update, ensure_ascii=False)[:1500])


def polling_loop() -> None:  # override to save bot username for PM deep links
    log.info("Deleting webhook before polling...")
    api.delete_webhook()
    me = api.get_me()
    set_bot_username_from_me(me)
    log.info("Bot started: %s", me)

    offset = None
    last_update_id = db.get_meta("last_update_id")
    if last_update_id and str(last_update_id).isdigit():
        offset = int(last_update_id) + 1

    if CLEAR_PENDING_ON_START:
        try:
            stale = api.get_updates(offset=-1, limit=1, timeout=1)
            if stale:
                newest = stale[-1].get("update_id")
                if newest is not None:
                    offset = int(newest) + 1
                    db.set_meta("last_update_id", newest)
                    log.warning("Cleared pending updates on startup. Next offset=%s", offset)
            else:
                log.info("No pending updates to clear on startup.")
        except Exception as exc:
            log.warning("Could not clear pending updates on startup: %s", exc)

    while RUNNING:
        try:
            updates = api.get_updates(offset=offset, timeout=30)
            for upd in updates:
                update_id = upd.get("update_id")
                if update_id is not None:
                    offset = int(update_id) + 1
                    db.set_meta("last_update_id", update_id)
                process_update(upd)
        except BaleAPIError as exc:
            log.warning("Polling API error: %s", exc)
            time.sleep(3)
        except Exception:
            log.exception("Polling loop crashed")
            time.sleep(3)
    log.info("Bot stopped.")




# ============================================================
# PM-only customization pack
# - Group-specific action/lock messages
# - Group-specific command aliases (e.g. "سیک" instead of "میوت")
# - Editing is done only in private chat; groups only show a PM entry point.
# ============================================================

_CUSTOM_OLD_PANEL_KEYBOARD = panel_keyboard
_CUSTOM_OLD_PRIVATE_MAIN_KEYBOARD = private_main_keyboard
_CUSTOM_OLD_OWNER_MAIN_KEYBOARD = owner_main_keyboard
_CUSTOM_OLD_HANDLE_START = handle_start
_CUSTOM_OLD_HANDLE_CALLBACK = handle_callback
_CUSTOM_OLD_GET_COMMAND = get_command
_CUSTOM_OLD_HANDLE_COMMAND = handle_command
_CUSTOM_OLD_PROCESS_PRIVATE_STATE = process_private_state
_CUSTOM_OLD_PROCESS_UPDATE = process_update

CUSTOM_TEXT_DEFS = {
    "mute_success": ("🔇 متن میوت", "🔇 {user} میوت شد.\nتا: {until}\nدلیل: {reason}"),
    "unmute_success": ("🔊 متن رفع میوت", "✅ {user} از میوت دراومد."),
    "ban_success": ("⛔ متن بن", "⛔ {user} بن شد.\nدلیل: {reason}"),
    "unban_success": ("✅ متن رفع بن", "✅ {user} آزاد شد."),
    "kick_success": ("👢 متن اخراج", "👢 {user} از گروه رفت بیرون.\nدلیل: {reason}"),
    "warn_success": ("⚠️ متن اخطار", "⚠️ {user} اخطار گرفت.\nدلیل: {reason}\nوضعیت: {warns}/{limit}"),
    "unwarn_success": ("🧹 متن پاک‌کردن اخطار", "✅ اخطارهای {user} پاک شد."),
    "warn_limit": ("🚦 متن سقف اخطار", "⚠️ {user} به سقف اخطار رسید ({warns}/{limit}) و {action}"),
    "lock_violation": ("🧹 متن حذف پیام قفل", "⚠️ {user} پیامش پاک شد.\nدلیل: {reason}\nاخطار: {warns}/{limit}"),
    "lock_limit": ("🚫 متن جریمه بعد از قفل", "🚫 {user} زیادی تخلف کرد و {action}.\nدلیل آخر: {reason}"),
    "lock_on": ("🔒 متن روشن شدن قفل", "✅ {item} قفل شد."),
    "lock_off": ("🔓 متن باز شدن قفل", "✅ {item} باز شد."),
    "all_locks_on": ("🔒 متن قفل همه", "✅ همه قفل‌ها روشن شدند."),
    "all_locks_off": ("🔓 متن باز کردن همه", "✅ همه قفل‌ها خاموش شدند."),
}

CUSTOM_COMMAND_DEFS = {
    "mute": ("میوت", "میوت کردن کاربر"),
    "unmute": ("رفع میوت", "برگرداندن کاربر از میوت"),
    "ban": ("بن", "بن کردن کاربر"),
    "unban": ("رفع بن", "آزاد کردن کاربر"),
    "kick": ("اخراج", "بیرون کردن کاربر"),
    "warn": ("اخطار", "اخطار دادن"),
    "unwarn": ("حذف اخطار", "پاک کردن اخطارها"),
    "warnings": ("اخطارها", "دیدن اخطارهای کاربر"),
}

CUSTOM_TONES = {
    "friendly": {
        "title": "😎 خودمونی",
        "texts": {
            "mute_success": "🔇 {user} یه مدت ساکت شد 😅\nتا: {until}\nدلیل: {reason}",
            "unmute_success": "✅ {user} برگشت، دیگه می‌تونه حرف بزنه.",
            "ban_success": "🚫 {user} از گروه حذف شد.\nدلیل: {reason}",
            "kick_success": "👢 {user} از گروه رفت بیرون.\nدلیل: {reason}",
            "warn_success": "⚠️ {user} یه اخطار گرفت.\nدلیل: {reason}\nوضعیت: {warns}/{limit}",
            "lock_violation": "پیامت پاک شد داداش 😅\nدلیل: {reason}\nاخطار: {warns}/{limit}",
        },
        "commands": {},
    },
    "fun": {
        "title": "😂 فان",
        "texts": {
            "mute_success": "{user} سیک شد 😂\nتا: {until}\nدلیل: {reason}",
            "unmute_success": "{user} از سیک دراومد، برگشت به زندگی 😄",
            "ban_success": "{user} پرتاب شد بیرون 🚀\nدلیل: {reason}",
            "kick_success": "{user} با احترام شوت شد بیرون 👢\nدلیل: {reason}",
            "warn_success": "{user} یه کارت زرد خورد 🟨\nدلیل: {reason}\nوضعیت: {warns}/{limit}",
            "lock_violation": "پیام {user} رفت هوا 🚀\nدلیل: {reason}\nاخطار: {warns}/{limit}",
            "lock_limit": "{user} زیادی شیطونی کرد و {action} 😅\nدلیل آخر: {reason}",
        },
        "commands": {"mute": "سیک", "unmute": "رفع سیک", "ban": "پرت کن بیرون", "kick": "شوت"},
    },
    "serious": {
        "title": "🛡 جدی",
        "texts": {
            "mute_success": "🔇 {user} محدود شد.\nمدت: {until}\nدلیل: {reason}",
            "ban_success": "⛔ {user} مسدود شد.\nدلیل: {reason}",
            "warn_success": "⚠️ برای {user} اخطار ثبت شد.\nدلیل: {reason}\nوضعیت: {warns}/{limit}",
            "lock_violation": "پیام {user} به دلیل نقض قوانین حذف شد.\nمورد: {reason}\nاخطار: {warns}/{limit}",
        },
        "commands": {},
    },
}

CUSTOM_LOCK_QUICK_KEYS = [
    "lock_links", "lock_ads", "lock_mentions", "lock_forwards", "lock_photos", "lock_videos",
    "lock_voice", "lock_files", "lock_stickers", "lock_bad_words", "anti_flood_enabled", "lock_duplicate_messages",
]


def ensure_customization_schema() -> None:
    with db._lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS custom_texts (
            group_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            text TEXT NOT NULL,
            updated_by INTEGER,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(group_id, key)
        );
        CREATE TABLE IF NOT EXISTS custom_commands (
            group_id INTEGER NOT NULL,
            command TEXT NOT NULL,
            alias TEXT NOT NULL,
            updated_by INTEGER,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(group_id, command)
        );
        CREATE INDEX IF NOT EXISTS idx_custom_commands_group_alias ON custom_commands(group_id, alias);
        """)


ensure_customization_schema()


def _custom_pm_allowed(uid: Optional[int], gid: int) -> bool:
    if not uid or not gid:
        return False
    if is_bot_owner(uid):
        return True
    try:
        return bool(can_manage(int(gid), int(uid), "panel"))
    except Exception:
        return False


def _custom_set_text(gid: int, key: str, text: str, by: Optional[int]) -> None:
    with db._lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO custom_texts(group_id,key,text,updated_by,updated_at) VALUES(?,?,?,?,?)",
            (int(gid), str(key), str(text)[:1200], int(by or 0), now_ts()),
        )


def _custom_delete_text(gid: int, key: str) -> None:
    with db._lock:
        db.conn.execute("DELETE FROM custom_texts WHERE group_id=? AND key=?", (int(gid), str(key)))


def _custom_get_text(gid: int, key: str, default: Optional[str] = None) -> str:
    row = db.conn.execute("SELECT text FROM custom_texts WHERE group_id=? AND key=?", (int(gid), str(key))).fetchone()
    if row:
        return row["text"]
    if default is not None:
        return default
    return CUSTOM_TEXT_DEFS.get(key, (key, ""))[1]


def _custom_set_cmd(gid: int, command: str, alias: str, by: Optional[int]) -> None:
    alias = fa_norm(alias)[:40]
    if not alias:
        return
    with db._lock:
        # هر اسم دستور فقط برای یک عملیات در همان گروه استفاده شود.
        db.conn.execute("DELETE FROM custom_commands WHERE group_id=? AND alias=?", (int(gid), alias))
        db.conn.execute(
            "INSERT OR REPLACE INTO custom_commands(group_id,command,alias,updated_by,updated_at) VALUES(?,?,?,?,?)",
            (int(gid), command, alias, int(by or 0), now_ts()),
        )


def _custom_delete_cmd(gid: int, command: str) -> None:
    with db._lock:
        db.conn.execute("DELETE FROM custom_commands WHERE group_id=? AND command=?", (int(gid), command))


def _custom_get_cmd_alias(gid: int, command: str) -> str:
    row = db.conn.execute("SELECT alias FROM custom_commands WHERE group_id=? AND command=?", (int(gid), command)).fetchone()
    if row and row["alias"]:
        return row["alias"]
    return CUSTOM_COMMAND_DEFS.get(command, (command, ""))[0]


def _custom_group_command(gid: int, text: str) -> Optional[Tuple[str, str]]:
    t = fa_norm(text or "")
    if not t:
        return None
    rows = [dict(r) for r in db.conn.execute("SELECT command,alias FROM custom_commands WHERE group_id=?", (int(gid),)).fetchall()]
    rows.sort(key=lambda r: len(fa_norm(r.get("alias") or "")), reverse=True)
    for r in rows:
        alias = fa_norm(r.get("alias") or "")
        if not alias:
            continue
        if t == alias:
            return r["command"], ""
        if t.startswith(alias + " "):
            return r["command"], t[len(alias):].strip()
    return None


def _render_custom_template(gid: int, key: str, data: dict, default: Optional[str] = None) -> str:
    tpl = _custom_get_text(gid, key, default)
    safe = {k: ("" if v is None else str(v)) for k, v in (data or {}).items()}
    try:
        return tpl.format(**safe)
    except Exception:
        # اگر کاربر آکولاد را اشتباه نوشت، متن خام را بفرست تا ربات نخوابد.
        return tpl


def _target_label_from_message(msg: dict, target: int) -> str:
    reply = msg.get("reply_to_message") or {}
    ruser = reply.get("from") or {}
    if ruser and user_id(ruser) == int(target):
        return first_name(ruser)
    return str(target)


def _ctx_for_action(cid: int, msg: Optional[dict] = None, target: Optional[int] = None, reason: str = "", **extra) -> dict:
    actor_user = msg_from(msg or {}) if msg else {}
    data = {
        "user": _target_label_from_message(msg or {}, target) if target else "کاربر",
        "user_id": target or "",
        "admin": first_name(actor_user) if actor_user else "ربات",
        "admin_id": user_id(actor_user) if actor_user else "",
        "reason": reason or "بدون دلیل",
        "group": db.get_group_title(int(cid)),
        "date": jalali_date(),
    }
    data.update(extra)
    return data


def customization_entry_text(gid: int) -> str:
    return (
        "🎨 شخصی‌سازی گروه\n\n"
        f"گروه: {db.get_group_title(int(gid))}\n"
        f"آیدی: <code>{gid}</code>\n\n"
        "از داخل پیوی می‌تونی متن‌های ربات و اسم دستورها رو عوض کنی.\n"
        "مثلاً اسم «میوت» رو بذاری «سیک» و متنش هم بشه «کاربر سیک شد» 😄"
    )


def customization_entry_keyboard(gid: int) -> dict:
    url = private_custom_url(gid)
    rows = []
    if url:
        rows.append([btn("🎨 تنظیم از پیوی ربات", url=url)])
    rows.append([btn("📋 کپی دستور پیوی", copy_text=f"شخصی سازی {gid}")])
    rows.append([btn("⬅️ برگشت", "panel:main")])
    return inline_keyboard(rows)


def private_custom_url(gid: int | str) -> Optional[str]:
    uname = get_bot_username()
    if not uname:
        return None
    return f"https://ble.ir/{uname}?start=custom_{gid}"


def custom_main_text(gid: int) -> str:
    return (
        "🎨 پنل شخصی‌سازی\n\n"
        f"گروه: {db.get_group_title(int(gid))}\n"
        f"آیدی گروه: <code>{gid}</code>\n\n"
        "اینجا فقط داخل پیوی تنظیم می‌کنی؛ داخل گروه فقط خروجی دیده میشه.\n"
        "متغیرهای قابل استفاده:\n"
        "<code>{user}</code> <code>{admin}</code> <code>{reason}</code> <code>{time}</code> <code>{until}</code> <code>{warns}</code> <code>{limit}</code> <code>{action}</code> <code>{group}</code>"
    )


def custom_main_keyboard(gid: int) -> dict:
    return inline_keyboard([
        [btn("🔇 متن عملیات‌ها", f"custom:texts:{gid}"), btn("🧹 متن قفل‌ها", f"custom:locks:{gid}")],
        [btn("⌨️ اسم دستورها", f"custom:cmds:{gid}"), btn("🎭 لحن آماده", f"custom:tones:{gid}")],
        [btn("👀 متغیرها و نمونه", f"custom:vars:{gid}"), btn("🔄 ریست همه", f"custom:resetall:{gid}")],
        [btn("⬅️ منوی اصلی", "pm:main")],
    ])


def custom_texts_menu(gid: int) -> tuple[str, dict]:
    lines = ["🔇 متن عملیات‌ها", "", "هرکدوم رو بزنی، متن جدید رو همینجا می‌فرستی.", ""]
    rows = []
    for key, (title, default) in CUSTOM_TEXT_DEFS.items():
        if key.startswith("lock_") or key in {"lock_violation", "lock_limit"}:
            continue
        cur = _custom_get_text(gid, key, default)
        changed = "✅" if cur != default else "▫️"
        lines.append(f"{changed} {title}")
        rows.append([btn(f"✏️ {title}", f"custom:settext:{gid}:{key}"), btn("🔄", f"custom:resettext:{gid}:{key}")])
    rows.append([btn("⬅️ برگشت", f"custom:open:{gid}")])
    return "\n".join(lines), inline_keyboard(rows)


def custom_locks_menu(gid: int) -> tuple[str, dict]:
    lines = ["🧹 متن قفل‌ها", "", "برای هر قفل می‌تونی پیام مخصوص بذاری. اگر چیزی تنظیم نکنی از متن عمومی حذف پیام استفاده میشه.", ""]
    rows = []
    rows.append([btn("✏️ متن عمومی حذف پیام", f"custom:settext:{gid}:lock_violation"), btn("🔄", f"custom:resettext:{gid}:lock_violation")])
    rows.append([btn("✏️ متن جریمه بعد از تکرار", f"custom:settext:{gid}:lock_limit"), btn("🔄", f"custom:resettext:{gid}:lock_limit")])
    for key in CUSTOM_LOCK_QUICK_KEYS:
        if key not in ALL_LOCK_KEYS:
            continue
        title = LOCK_NAME_BY_KEY.get(key, key)
        tkey = f"violation_{key}"
        row = db.conn.execute("SELECT text FROM custom_texts WHERE group_id=? AND key=?", (int(gid), tkey)).fetchone()
        lines.append(f"{'✅' if row else '▫️'} {title}")
        rows.append([btn(f"✏️ قفل {title}", f"custom:settext:{gid}:{tkey}"), btn("🔄", f"custom:resettext:{gid}:{tkey}")])
    rows.append([btn("⬅️ برگشت", f"custom:open:{gid}")])
    return "\n".join(lines), inline_keyboard(rows)


def custom_commands_menu(gid: int) -> tuple[str, dict]:
    lines = ["⌨️ اسم دستورها", "", "اینجا اسم دستورهای مدیریت کاربر رو برای همین گروه عوض می‌کنی.", "مثال: میوت → سیک", ""]
    rows = []
    for cmd, (default_alias, desc) in CUSTOM_COMMAND_DEFS.items():
        alias = _custom_get_cmd_alias(gid, cmd)
        changed = "✅" if fa_norm(alias) != fa_norm(default_alias) else "▫️"
        lines.append(f"{changed} {desc}: {alias}")
        rows.append([btn(f"✏️ {default_alias}", f"custom:setcmd:{gid}:{cmd}"), btn("🔄", f"custom:resetcmd:{gid}:{cmd}")])
    rows.append([btn("⬅️ برگشت", f"custom:open:{gid}")])
    return "\n".join(lines), inline_keyboard(rows)


def custom_tones_menu(gid: int) -> tuple[str, dict]:
    rows = []
    for tone, data in CUSTOM_TONES.items():
        rows.append([btn(data["title"], f"custom:tone:{gid}:{tone}")])
    rows.append([btn("⬅️ برگشت", f"custom:open:{gid}")])
    return "🎭 لحن آماده\n\nبا یک دکمه، چند متن و چند اسم دستور با هم ست میشن. بعداً هرکدوم رو خواستی جدا تغییر بده.", inline_keyboard(rows)


def custom_vars_text(gid: int) -> str:
    return (
        "👀 متغیرهای قابل استفاده\n\n"
        "<code>{user}</code> نام/آیدی کاربر\n"
        "<code>{user_id}</code> آیدی کاربر\n"
        "<code>{admin}</code> اسم مدیری که دستور داده\n"
        "<code>{reason}</code> دلیل\n"
        "<code>{time}</code> مدت\n"
        "<code>{until}</code> زمان پایان\n"
        "<code>{warns}</code> تعداد اخطار\n"
        "<code>{limit}</code> سقف اخطار\n"
        "<code>{action}</code> جریمه انجام‌شده\n"
        "<code>{group}</code> اسم گروه\n"
        "<code>{date}</code> تاریخ\n\n"
        "نمونه:\n"
        "<code>{user} سیک شد 😂 دلیل: {reason}</code>"
    )


def render_custom_pm_section(chat_id_: int, mid: Optional[int], uid: int, gid: int, section: str = "main") -> None:
    if not _custom_pm_allowed(uid, gid):
        api.safe_send_message(chat_id_, "برای این گروه دسترسی تنظیمات نداری 😅\nیا ربات هنوز ادمین نیست، یا خودت مدیر گروه نیستی.")
        return
    if section == "main":
        _edit_or_send_panel(chat_id_, mid, custom_main_text(gid), custom_main_keyboard(gid)); return
    if section == "texts":
        text, kb = custom_texts_menu(gid); _edit_or_send_panel(chat_id_, mid, text, kb); return
    if section == "locks":
        text, kb = custom_locks_menu(gid); _edit_or_send_panel(chat_id_, mid, text, kb); return
    if section == "cmds":
        text, kb = custom_commands_menu(gid); _edit_or_send_panel(chat_id_, mid, text, kb); return
    if section == "tones":
        text, kb = custom_tones_menu(gid); _edit_or_send_panel(chat_id_, mid, text, kb); return
    if section == "vars":
        _edit_or_send_panel(chat_id_, mid, custom_vars_text(gid), inline_keyboard([[btn("⬅️ برگشت", f"custom:open:{gid}")]])); return


def _apply_custom_tone(gid: int, tone: str, by: Optional[int]) -> bool:
    data = CUSTOM_TONES.get(tone)
    if not data:
        return False
    for key, value in data.get("texts", {}).items():
        _custom_set_text(gid, key, value, by)
    for cmd, alias in data.get("commands", {}).items():
        _custom_set_cmd(gid, cmd, alias, by)
    return True


def _reset_all_customization(gid: int) -> None:
    with db._lock:
        db.conn.execute("DELETE FROM custom_texts WHERE group_id=?", (int(gid),))
        db.conn.execute("DELETE FROM custom_commands WHERE group_id=?", (int(gid),))


def panel_keyboard(group_id: Optional[int] = None) -> dict:  # override: add customization entry in group panel
    return inline_keyboard([
        [btn("🔒 قفل‌ها", "locks:core"), btn("⚠️ اخطارها", "p:warn")],
        [btn("📋 لاگ‌ها", "p:logs"), btn("📊 آمار", "p:stats")],
        [btn("📢 جوین اجباری", "p:force"), btn("👋 خوشامد/قوانین", "p:welcome")],
        [btn("👮 مدیرها", "p:roles"), btn("💾 بکاپ/ریستور", "p:backup")],
        [btn("💎 اشتراک", "p:premium"), btn("💳 پرداخت", "p:payment")],
        [btn("🎨 شخصی‌سازی", "custom:group"), btn("🚨 ضدحمله", "p:attack")],
        [btn("📋 کپی راهنما", copy_text="راهنما")],
    ])


def private_main_keyboard(uid: Optional[int] = None) -> dict:  # override: add customization entry in private
    rows = [
        [btn("🛒 خرید اشتراک", "buy:start:0")],
        [btn("🎨 شخصی‌سازی گروه", "custom:start:0")],
        [btn("📦 سفارش‌های من", "pm:myorders")],
    ]
    if is_bot_owner(uid):
        rows.insert(0, [btn("👑 پنل مالک", "owner:main")])
    return inline_keyboard(rows)


def owner_main_keyboard() -> dict:  # override: owner also sees customization shortcut
    return inline_keyboard([
        [btn("💳 تنظیمات پرداخت", "owner:payments"), btn("💎 پلن‌ها", "owner:plans")],
        [btn("🧾 رسیدها", "owner:receipts"), btn("📦 سفارش‌ها", "owner:orders")],
        [btn("👥 گروه‌های فعال", "owner:groups"), btn("📊 آمار فروش", "owner:sales")],
        [btn("🎨 شخصی‌سازی گروه", "custom:start:0"), btn("⚙️ تنظیمات کلی", "owner:general")],
    ])


def handle_start(msg: dict, args: str) -> None:  # override: support custom_<group_id> deeplink
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if is_private(msg):
        a = (args or "").strip()
        m = re.search(r"custom_(-?\d+)", a)
        if m:
            gid = int(m.group(1))
            render_custom_pm_section(cid, None, uid or 0, gid, "main")
            return
    return _CUSTOM_OLD_HANDLE_START(msg, args)


def get_command(text: str) -> Optional[Tuple[str, str]]:  # override: PM command for customization
    base = _CUSTOM_OLD_GET_COMMAND(text)
    if base:
        return base
    t = fa_norm(text or "")
    if not t:
        return None
    if t.startswith("شخصی سازی") or t.startswith("شخصی‌سازی") or t.startswith("تنظیم متن"):
        rest = re.sub(r"^(شخصی سازی|شخصی‌سازی|تنظیم متن)\s*", "", t).strip()
        return "customize", rest
    return None


def handle_command(msg: dict, cmd: str, args: str) -> None:  # override: customization command
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if cmd == "customize":
        nums = re.findall(r"-?\d+", args or "")
        if is_group(msg):
            if not require_admin(msg, "panel"):
                return
            api.safe_send_message(cid, customization_entry_text(int(cid)), parse_mode="HTML", reply_to_message_id=message_id(msg), reply_markup=customization_entry_keyboard(int(cid)))
            return
        if not nums:
            set_state(uid or 0, "await_custom_group_id", {})
            api.safe_send_message(cid, "آیدی عددی گروه رو بفرست تا پنل شخصی‌سازی همون گروه رو باز کنم.\nراحت‌ترش: داخل گروه بزن «پنل» و دکمه 🎨 شخصی‌سازی رو بزن.", reply_markup=private_main_keyboard(uid))
            return
        gid = int(nums[0])
        render_custom_pm_section(cid, None, uid or 0, gid, "main")
        return
    return _CUSTOM_OLD_HANDLE_COMMAND(msg, cmd, args)


def process_private_state(msg: dict, state_row: dict) -> bool:  # override: PM customization states
    uid = user_id(msg_from(msg)); cid = chat_id(msg)
    if not uid:
        return False
    state = state_row.get("state")
    data = state_row.get("data") or {}
    text = get_text(msg).strip()
    if state and state.startswith("custom_") or state == "await_custom_group_id":
        if fa_norm(text) in {"لغو", "انصراف", "cancel"}:
            clear_state(uid)
            api.safe_send_message(cid, "لغو شد ✅", reply_markup=private_main_keyboard(uid))
            return True
    if state == "await_custom_group_id":
        nums = re.findall(r"-?\d+", text)
        if not nums:
            api.safe_send_message(cid, "آیدی عددی گروه رو بفرست. مثال:\n<code>-100123456789</code>", parse_mode="HTML")
            return True
        gid = int(nums[0])
        clear_state(uid)
        render_custom_pm_section(cid, None, uid, gid, "main")
        return True
    if state == "custom_set_text":
        gid = int(data.get("group_id") or 0); key = str(data.get("key") or "")
        if not gid or not key or not _custom_pm_allowed(uid, gid):
            clear_state(uid); api.safe_send_message(cid, "دسترسی یا اطلاعات تنظیمات پیدا نشد 😕", reply_markup=private_main_keyboard(uid)); return True
        if len(text) > 1200:
            api.safe_send_message(cid, "متن خیلی بلنده؛ زیر ۱۲۰۰ کاراکتر بفرست.")
            return True
        _custom_set_text(gid, key, text, uid); clear_state(uid)
        api.safe_send_message(cid, "متن ذخیره شد ✅\nاز این به بعد تو همون گروه با متن جدید جواب میدم.", reply_markup=custom_main_keyboard(gid))
        return True
    if state == "custom_set_cmd":
        gid = int(data.get("group_id") or 0); command = str(data.get("command") or "")
        if not gid or command not in CUSTOM_COMMAND_DEFS or not _custom_pm_allowed(uid, gid):
            clear_state(uid); api.safe_send_message(cid, "دسترسی یا اطلاعات دستور پیدا نشد 😕", reply_markup=private_main_keyboard(uid)); return True
        alias = fa_norm(text)
        if len(alias) > 40:
            api.safe_send_message(cid, "اسم دستور خیلی بلنده؛ کوتاه‌تر بفرست.")
            return True
        if not alias:
            api.safe_send_message(cid, "اسم دستور خالیه؛ دوباره بفرست.")
            return True
        _custom_set_cmd(gid, command, alias, uid); clear_state(uid)
        api.safe_send_message(cid, f"اوکی ✅\nاز این به بعد «{alias}» همون کار «{CUSTOM_COMMAND_DEFS[command][0]}» رو انجام میده.", reply_markup=custom_main_keyboard(gid))
        return True
    return _CUSTOM_OLD_PROCESS_PRIVATE_STATE(msg, state_row)


def handle_callback(update: dict) -> bool:  # override: PM customization callbacks
    q = update.get("callback_query")
    if not q:
        return False
    data = q.get("data") or ""
    msg = q.get("message") or {}
    cid = chat_id(msg); mid = message_id(msg); actor = user_id(q.get("from") or {})
    qid = q.get("id") or q.get("callback_query_id")

    if data.startswith("custom:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        answered = False

        def _ans(text: str = "", alert: bool = False):
            nonlocal answered
            if qid and not answered:
                api.answer_callback_query(qid, text, alert)
                answered = True

        if action == "group" and cid and is_group(msg):
            if not can_manage(int(cid), actor, "panel"):
                _ans("این بخش برای مدیرهای گروهه 😅", True)
                return True
            _ans()
            api.safe_send_message(cid, customization_entry_text(int(cid)), parse_mode="HTML", reply_markup=customization_entry_keyboard(int(cid)))
            return True

        if action == "start":
            _ans()
            if not is_private(msg):
                api.safe_send_message(cid, "تنظیمات شخصی‌سازی فقط توی پیوی انجام میشه 😅")
                return True
            set_state(actor or 0, "await_custom_group_id", {})
            api.safe_send_message(cid, "آیدی عددی گروه رو بفرست تا پنل شخصی‌سازی رو باز کنم.\nیا از داخل گروه دکمه 🎨 شخصی‌سازی رو بزن.", reply_markup=private_main_keyboard(actor))
            return True

        # از اینجا به بعد فقط پیوی و با آیدی گروه معتبر
        if not is_private(msg):
            _ans()
            api.safe_send_message(cid, "برای تغییر متن‌ها برو پیوی ربات؛ اینجا فقط دکمه ورود رو میدم.")
            return True
        if len(parts) < 3:
            return True
        try:
            gid = int(parts[2])
        except Exception:
            return True
        if not _custom_pm_allowed(actor, gid):
            _ans("دسترسی نداری 😅", True)
            api.safe_send_message(cid, "برای این گروه دسترسی نداری یا ربات هنوز ادمین گروه نیست 😕")
            return True
        _ans()

        if action == "open": render_custom_pm_section(cid, mid, actor or 0, gid, "main"); return True
        if action == "texts": render_custom_pm_section(cid, mid, actor or 0, gid, "texts"); return True
        if action == "locks": render_custom_pm_section(cid, mid, actor or 0, gid, "locks"); return True
        if action == "cmds": render_custom_pm_section(cid, mid, actor or 0, gid, "cmds"); return True
        if action == "tones": render_custom_pm_section(cid, mid, actor or 0, gid, "tones"); return True
        if action == "vars": render_custom_pm_section(cid, mid, actor or 0, gid, "vars"); return True
        if action == "settext" and len(parts) >= 4:
            key = parts[3]
            title = CUSTOM_TEXT_DEFS.get(key, (LOCK_NAME_BY_KEY.get(key.replace("violation_", ""), key), ""))[0]
            current = _custom_get_text(gid, key, CUSTOM_TEXT_DEFS.get(key, ("", ""))[1])
            set_state(actor or 0, "custom_set_text", {"group_id": gid, "key": key})
            api.safe_send_message(cid, f"متن جدید برای «{title}» رو بفرست.\n\nمتن فعلی:\n{current}\n\nبرای لغو بنویس: لغو", reply_markup=inline_keyboard([[btn("📋 متغیرها", f"custom:vars:{gid}")]]))
            return True
        if action == "resettext" and len(parts) >= 4:
            _custom_delete_text(gid, parts[3]); render_custom_pm_section(cid, mid, actor or 0, gid, "main"); api.safe_send_message(cid, "متن برگشت به پیش‌فرض ✅"); return True
        if action == "setcmd" and len(parts) >= 4:
            command = parts[3]
            if command not in CUSTOM_COMMAND_DEFS:
                return True
            set_state(actor or 0, "custom_set_cmd", {"group_id": gid, "command": command})
            api.safe_send_message(cid, f"اسم جدید برای دستور «{CUSTOM_COMMAND_DEFS[command][0]}» رو بفرست.\nمثال: سیک\n\nبرای لغو بنویس: لغو")
            return True
        if action == "resetcmd" and len(parts) >= 4:
            _custom_delete_cmd(gid, parts[3]); render_custom_pm_section(cid, mid, actor or 0, gid, "cmds"); api.safe_send_message(cid, "اسم دستور برگشت به پیش‌فرض ✅"); return True
        if action == "tone" and len(parts) >= 4:
            tone = parts[3]
            if _apply_custom_tone(gid, tone, actor):
                api.safe_send_message(cid, f"لحن {CUSTOM_TONES[tone]['title']} برای گروه تنظیم شد ✅", reply_markup=custom_main_keyboard(gid))
            return True
        if action == "resetall":
            _reset_all_customization(gid)
            api.safe_send_message(cid, "همه شخصی‌سازی‌های این گروه برگشت به پیش‌فرض ✅", reply_markup=custom_main_keyboard(gid))
            return True
        return True

    return _CUSTOM_OLD_HANDLE_CALLBACK(update)


def warn_user_smart(cid: int, uid: int, reason: str, actor: Optional[int], public_name: str = "کاربر") -> str:  # override templates
    count = db.warn_user(cid, uid, reason, actor)
    ext_record_daily(cid, uid, warn_delta=1)
    limit = int(_settings(cid).get("warn_limit", 3))
    send_group_log(cid, "اخطار ثبت شد", f"برای: <code>{uid}</code>\nدلیل: {reason}\nوضعیت: {count}/{limit}")
    ctx = {"user": public_name, "user_id": uid, "reason": reason, "warns": count, "limit": limit, "group": db.get_group_title(int(cid)), "date": jalali_date()}
    if count >= limit:
        result = _apply_warn_punishment(cid, uid, reason, actor)
        ctx["action"] = result
        return _render_custom_template(cid, "warn_limit", ctx)
    return _render_custom_template(cid, "warn_success", ctx)


def handle_lock(msg: dict, cmd: str, args: str) -> None:  # override templates for lock/unlock responses
    if not require_admin(msg, "locks"):
        return
    cid = int(chat_id(msg)); target = _normalize_lock_target(args or ""); val = cmd == "lock"
    if target in {"all", "همه"}:
        for k in ALL_LOCK_KEYS:
            db.update_group_setting(cid, k, val)
        key = "all_locks_on" if val else "all_locks_off"
        text = _render_custom_template(cid, key, _ctx_for_action(cid, msg, item="همه قفل‌ها"))
        api.safe_send_message(cid, text, reply_to_message_id=message_id(msg), reply_markup=locks_panel_keyboard(db.get_group_settings(cid)))
        return
    key = LOCK_ALIAS_TO_KEY.get(fa_norm(target), target)
    if key not in ALL_LOCK_KEYS:
        api.safe_send_message(cid, "گزینه درست نیست. مثال: قفل لینک، قفل عکس، قفل تبلیغ، قفل فلود، قفل همه", reply_to_message_id=message_id(msg)); return
    db.update_group_setting(cid, key, val)
    name = LOCK_NAME_BY_KEY.get(key, target)
    tkey = "lock_on" if val else "lock_off"
    text = _render_custom_template(cid, tkey, _ctx_for_action(cid, msg, item=name))
    api.safe_send_message(cid, text, reply_to_message_id=message_id(msg), reply_markup=locks_panel_keyboard(db.get_group_settings(cid), _find_lock_category(key)))


def handle_moderation(msg: dict, cmd: str, args: str) -> None:  # override templates for manual moderation
    if not require_admin(msg, "warns"):
        return
    cid = int(chat_id(msg)); actor = user_id(msg_from(msg)); target, reason = get_target_from_reply_or_arg(msg, args)
    if not target:
        api.safe_send_message(cid, "روی پیام طرف ریپلای کن یا آیدیش رو بده.", reply_to_message_id=message_id(msg)); return
    if can_manage(cid, target, "panel"):
        api.safe_send_message(cid, "روی مدیرهای گروه کاری انجام نمیدم 😅", reply_to_message_id=message_id(msg)); return
    reason = reason or "بدون دلیل"
    user_label = _target_label_from_message(msg, target)
    ctx = _ctx_for_action(cid, msg, target, reason)
    try:
        if cmd == "ban":
            api.ban_chat_member(cid, target); db.log(cid, target, "ban", {"admin_id": actor, "reason": reason}); send_group_log(cid, "بن دستی", f"کاربر: <code>{target}</code>\nدلیل: {reason}")
            api.safe_send_message(cid, _render_custom_template(cid, "ban_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "unban":
            api.unban_chat_member(cid, target, only_if_banned=True); db.log(cid, target, "unban", {"admin_id": actor}); send_group_log(cid, "رفع بن", f"کاربر: <code>{target}</code>")
            api.safe_send_message(cid, _render_custom_template(cid, "unban_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "kick":
            api.ban_chat_member(cid, target); time.sleep(0.3); api.unban_chat_member(cid, target, only_if_banned=False); db.log(cid, target, "kick", {"admin_id": actor, "reason": reason}); send_group_log(cid, "اخراج دستی", f"کاربر: <code>{target}</code>\nدلیل: {reason}")
            api.safe_send_message(cid, _render_custom_template(cid, "kick_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "warn":
            api.safe_send_message(cid, warn_user_smart(cid, target, reason, actor, user_label), reply_to_message_id=message_id(msg))
        elif cmd == "unwarn":
            db.clear_warnings(cid, target); api.safe_send_message(cid, _render_custom_template(cid, "unwarn_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "warnings":
            data = db.get_warnings(cid, target); lines = [f"⚠️ اخطارهای {user_label}: {data['count']}"]
            for item in data["reasons"][-10:]: lines.append(f"• {item.get('date','')} — {item.get('reason','')}")
            api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg))
        elif cmd == "mute":
            until_ts, rem = parse_duration(reason, default_seconds=3600); db.mute_user(cid, target, until_ts, rem or "میوت دستی"); until_text = jalali_date(until_ts) if until_ts else "نامحدود"; send_group_log(cid, "میوت دستی", f"کاربر: <code>{target}</code>\nتا: {until_text}")
            ctx.update({"time": until_text, "until": until_text, "reason": rem or reason or "میوت دستی"})
            api.safe_send_message(cid, _render_custom_template(cid, "mute_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "unmute":
            ok = db.unmute_user(cid, target)
            api.safe_send_message(cid, _render_custom_template(cid, "unmute_success", ctx) if ok else "این کاربر میوت نبود.", reply_to_message_id=message_id(msg))
    except BaleAPIError as exc:
        api.safe_send_message(cid, f"انجام نشد 😕\n{exc.description}\nاحتمالاً دسترسی ادمین ربات کافی نیست.", reply_to_message_id=message_id(msg))


def moderate_message(msg: dict, settings: dict) -> bool:  # override: lock-specific custom messages
    cid = int(chat_id(msg)); mid = message_id(msg); user = msg_from(msg); uid = user_id(user)
    if not uid or can_manage(cid, uid, "panel"):
        return False
    if db.is_muted(cid, uid):
        api.safe_delete_message(cid, mid); ext_record_daily(cid, uid, deleted_delta=1); return True
    text = get_text(msg) or ""
    violations: list[tuple[str, str]] = []
    def v(key: str, label: str): violations.append((key, label))

    if settings.get("lock_chat"): v("lock_chat", "قفل چت")
    if settings.get("lock_night") and _is_night_locked(settings): v("lock_night", "قفل شبانه")
    if settings.get("lock_new_members"):
        joined_at = recent_join_cache.get((cid, uid))
        if joined_at and now_ts() - joined_at <= int(settings.get("new_member_watch_seconds", 300)): v("lock_new_members", "محدودیت عضو جدید")
    if settings.get("lock_fake_accounts") and _is_fake_account(user): v("lock_fake_accounts", "اکانت فیک")
    if settings.get("lock_suspicious_names") and _is_suspicious_name(user): v("lock_suspicious_names", "اسم مشکوک")
    if settings.get("lock_links") and LINK_RE.search(text): v("lock_links", "لینک")
    if settings.get("lock_bale_links") and BALE_LINK_RE.search(text): v("lock_bale_links", "لینک بله")
    if settings.get("lock_telegram_links") and TELEGRAM_LINK_RE.search(text): v("lock_telegram_links", "لینک تلگرام")
    if settings.get("lock_instagram_links") and INSTAGRAM_LINK_RE.search(text): v("lock_instagram_links", "لینک اینستاگرام")
    if settings.get("lock_whatsapp_links") and WHATSAPP_LINK_RE.search(text): v("lock_whatsapp_links", "لینک واتساپ")
    if settings.get("lock_site_links") and SITE_LINK_RE.search(text): v("lock_site_links", "لینک سایت")
    if settings.get("lock_channels") and CHANNEL_RE.search(text): v("lock_channels", "کانال/یوزرنیم")
    if settings.get("lock_mentions") and MENTION_RE.search(text): v("lock_mentions", "منشن")
    if settings.get("lock_usernames") and MENTION_RE.search(text): v("lock_usernames", "آیدی/یوزرنیم")
    if settings.get("lock_phone_numbers") and PHONE_RE.search(normalize_digits(text)): v("lock_phone_numbers", "شماره")
    if settings.get("lock_ads") and (AD_RE.search(text) and (LINK_RE.search(text) or MENTION_RE.search(text) or PHONE_RE.search(normalize_digits(text)))): v("lock_ads", "تبلیغ")
    if settings.get("lock_media") and has_media(msg): v("lock_media", "رسانه")
    if settings.get("lock_photos") and _message_has_photo(msg): v("lock_photos", "عکس")
    if settings.get("lock_videos") and _message_has_video(msg): v("lock_videos", "ویدیو")
    if settings.get("lock_voice") and _message_has_voice(msg): v("lock_voice", "ویس")
    if settings.get("lock_audio") and _message_has_audio(msg): v("lock_audio", "موزیک/صدا")
    if settings.get("lock_files") and _message_has_file(msg): v("lock_files", "فایل")
    if settings.get("lock_stickers") and has_sticker(msg): v("lock_stickers", "استیکر")
    if settings.get("lock_gifs") and _message_has_gif(msg): v("lock_gifs", "گیف")
    if settings.get("lock_contacts") and _message_has_contact(msg): v("lock_contacts", "مخاطب")
    if settings.get("lock_locations") and _message_has_location(msg): v("lock_locations", "لوکیشن")
    if settings.get("lock_forwards") and is_forwarded(msg): v("lock_forwards", "فوروارد")
    if settings.get("lock_duplicate_messages") and _record_duplicate_and_check(cid, uid, text): v("lock_duplicate_messages", "پیام تکراری")
    if settings.get("lock_long_text") and len(text) > int(settings.get("long_text_limit", 500)): v("lock_long_text", "متن طولانی")
    if settings.get("lock_short_text") and text and len(fa_norm(text)) <= int(settings.get("short_text_limit", 2)): v("lock_short_text", "پیام کوتاه")
    if settings.get("lock_emoji_spam") and len(EMOJI_RE.findall(text)) > int(settings.get("emoji_limit", 10)): v("lock_emoji_spam", "ایموجی زیاد")
    if settings.get("lock_stretched_chars") and STRETCHED_RE.search(text): v("lock_stretched_chars", "حروف کشیده")
    if settings.get("lock_bad_words") and _has_bad_word(text): v("lock_bad_words", "فحش")
    if settings.get("anti_flood_enabled"):
        key = (cid, uid); q = flood_cache[key]; t = now_ts(); window = int(settings.get("flood_window") or DEFAULT_FLOOD_WINDOW); limit = int(settings.get("flood_limit") or DEFAULT_FLOOD_LIMIT)
        q.append(t)
        while q and t - q[0] > window: q.popleft()
        if len(q) > limit: v("anti_flood_enabled", "فلود")

    if not violations:
        return False
    api.safe_delete_message(cid, mid); ext_record_daily(cid, uid, deleted_delta=1)
    unique = []
    for key, label in violations:
        if (key, label) not in unique:
            unique.append((key, label))
    reason = "، ".join(label for _key, label in unique)
    count = db.warn_user(cid, uid, reason, None); ext_record_daily(cid, uid, warn_delta=1)
    limit = int(settings.get("warn_limit", 3))
    send_group_log(cid, "پیام حذف شد", f"برای: <code>{uid}</code>\nدلیل: {reason}\nوضعیت اخطار: {count}/{limit}")
    ctx = {"user": first_name(user), "user_id": uid, "reason": reason, "warns": count, "limit": limit, "group": db.get_group_title(int(cid)), "date": jalali_date()}
    if count >= limit:
        result = _apply_warn_punishment(cid, uid, reason, None)
        ctx["action"] = result
        api.safe_send_message(cid, _render_custom_template(cid, "lock_limit", ctx))
    else:
        text_key = None
        if len(unique) == 1:
            specific_key = f"violation_{unique[0][0]}"
            row = db.conn.execute("SELECT 1 FROM custom_texts WHERE group_id=? AND key=?", (cid, specific_key)).fetchone()
            if row:
                text_key = specific_key
        api.safe_send_message(cid, _render_custom_template(cid, text_key or "lock_violation", ctx, CUSTOM_TEXT_DEFS["lock_violation"][1]))
    return True


def process_update(update: dict) -> None:  # override: custom group aliases before standard commands
    try:
        _maybe_auto_backup()
        if handle_payment_updates(update): return
        if handle_callback(update): return
        msg = get_message(update)
        if not msg: return
        if is_private(msg):
            uid = user_id(msg_from(msg)); st = get_state(uid) if uid else None
            if st and process_private_state(msg, st):
                return
        text = get_text(msg)
        command = None
        if is_group(msg) and text:
            try:
                command = _custom_group_command(int(chat_id(msg)), text)
            except Exception:
                command = None
        if not command:
            command = get_command(text)
        if command:
            handle_command(msg, command[0], command[1]); return
        if is_group(msg):
            handle_normal_group_message(msg)
        elif is_private(msg) and text:
            api.safe_send_message(chat_id(msg), private_main_text(msg_from(msg)), reply_markup=private_main_keyboard(user_id(msg_from(msg))))
    except Exception:
        log.exception("Update processing failed: %s", json.dumps(update, ensure_ascii=False)[:1500])


# ============================================================
# User experience pack
# - User profile, points, levels
# - Report violations to admins
# - Appeal warnings/mutes in private chat
# - Smart button help
# - PM reset for customized texts/commands
# ============================================================
import html as _html

_USER_OLD_GET_COMMAND = get_command
_USER_OLD_HANDLE_COMMAND = handle_command
_USER_OLD_HANDLE_CALLBACK = handle_callback
_USER_OLD_HANDLE_START = handle_start
_USER_OLD_PROCESS_PRIVATE_STATE = process_private_state
_USER_OLD_MODERATE_MESSAGE = moderate_message
_USER_OLD_HANDLE_NORMAL_GROUP_MESSAGE = handle_normal_group_message
_USER_OLD_WARN_USER_SMART = warn_user_smart
_USER_OLD_CUSTOM_MAIN_KEYBOARD = custom_main_keyboard

LEVELS = [
    (0, "🌱 تازه‌وارد"),
    (30, "🙂 معمولی"),
    (100, "🔥 فعال"),
    (250, "⭐ کاربر خوب"),
    (500, "👑 عضو ویژه"),
    (1000, "🏆 افسانه گروه"),
]


def ensure_user_features_schema() -> None:
    with db._lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            first_name TEXT,
            username TEXT,
            score INTEGER NOT NULL DEFAULT 0,
            risk_score INTEGER NOT NULL DEFAULT 0,
            msg_count INTEGER NOT NULL DEFAULT 0,
            deleted_count INTEGER NOT NULL DEFAULT 0,
            warnings_count INTEGER NOT NULL DEFAULT 0,
            reports_sent INTEGER NOT NULL DEFAULT 0,
            reports_confirmed INTEGER NOT NULL DEFAULT 0,
            appeals_count INTEGER NOT NULL DEFAULT 0,
            joined_at INTEGER,
            last_seen INTEGER NOT NULL,
            PRIMARY KEY(group_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS moderation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            admin_id INTEGER,
            event_type TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_mod_events_group_user ON moderation_events(group_id, user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS user_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            reported_id INTEGER,
            message_id INTEGER,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_reports_group_status ON user_reports(group_id, status, created_at DESC);
        CREATE TABLE IF NOT EXISTS user_appeals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            handled_by INTEGER,
            handled_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_appeals_group_status ON user_appeals(group_id, status, created_at DESC);
        """)


ensure_user_features_schema()


def _h(s: str) -> str:
    return _html.escape(str(s or ""), quote=False)


def _level_for_score(score: int) -> str:
    title = LEVELS[0][1]
    for need, name in LEVELS:
        if int(score) >= need:
            title = name
    return title


def _upsert_profile(cid: int, user: dict | int, *, msg_delta: int = 0, score_delta: int = 0,
                    risk_delta: int = 0, deleted_delta: int = 0, warn_delta: int = 0,
                    report_delta: int = 0, report_confirm_delta: int = 0,
                    appeal_delta: int = 0) -> None:
    if isinstance(user, dict):
        uid = user_id(user)
        fname = first_name(user)
        uname = username(user)
    else:
        uid = int(user or 0)
        fname = ""
        uname = ""
    if not cid or not uid:
        return
    ts = now_ts()
    with db._lock:
        db.conn.execute(
            """
            INSERT INTO user_profiles(group_id,user_id,first_name,username,score,risk_score,msg_count,deleted_count,warnings_count,reports_sent,reports_confirmed,appeals_count,joined_at,last_seen)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id,user_id) DO UPDATE SET
                first_name=COALESCE(NULLIF(excluded.first_name,''), user_profiles.first_name),
                username=COALESCE(NULLIF(excluded.username,''), user_profiles.username),
                score=MAX(0, user_profiles.score + excluded.score),
                risk_score=MAX(0, user_profiles.risk_score + excluded.risk_score),
                msg_count=user_profiles.msg_count + excluded.msg_count,
                deleted_count=user_profiles.deleted_count + excluded.deleted_count,
                warnings_count=user_profiles.warnings_count + excluded.warnings_count,
                reports_sent=user_profiles.reports_sent + excluded.reports_sent,
                reports_confirmed=user_profiles.reports_confirmed + excluded.reports_confirmed,
                appeals_count=user_profiles.appeals_count + excluded.appeals_count,
                last_seen=excluded.last_seen
            """,
            (int(cid), int(uid), fname, uname, int(score_delta), int(risk_delta), int(msg_delta),
             int(deleted_delta), int(warn_delta), int(report_delta), int(report_confirm_delta), int(appeal_delta), ts, ts),
        )


def _profile_row(cid: int, uid: int) -> dict:
    row = db.conn.execute("SELECT * FROM user_profiles WHERE group_id=? AND user_id=?", (int(cid), int(uid))).fetchone()
    if row:
        return dict(row)
    return {
        "group_id": int(cid), "user_id": int(uid), "first_name": "کاربر", "username": "",
        "score": 0, "risk_score": 0, "msg_count": 0, "deleted_count": 0,
        "warnings_count": 0, "reports_sent": 0, "reports_confirmed": 0,
        "appeals_count": 0, "joined_at": now_ts(), "last_seen": now_ts(),
    }


def _profile_text(cid: int, uid: int, display_name: str = "") -> str:
    row = _profile_row(cid, uid)
    warns = db.get_warnings(int(cid), int(uid)).get("count", 0)
    score = int(row.get("score") or 0)
    risk = int(row.get("risk_score") or 0)
    level = _level_for_score(score)
    name = display_name or row.get("first_name") or str(uid)
    risk_label = "عادی ✅"
    if risk >= 100:
        risk_label = "پرریسک ⚠️"
    elif risk >= 50:
        risk_label = "مشکوک 👀"
    return (
        f"👤 پروفایل {name}\n\n"
        f"🏅 سطح: {level}\n"
        f"⭐ امتیاز: {score}\n"
        f"💬 پیام‌ها: {int(row.get('msg_count') or 0)}\n"
        f"⚠️ اخطارها: {warns}\n"
        f"🧹 پیام‌های حذف‌شده: {int(row.get('deleted_count') or 0)}\n"
        f"🚨 گزارش‌های ارسال‌شده: {int(row.get('reports_sent') or 0)}\n"
        f"✅ گزارش‌های درست: {int(row.get('reports_confirmed') or 0)}\n"
        f"📊 وضعیت ریسک: {risk_label}\n"
        f"📅 آخرین فعالیت: {jalali_date(int(row.get('last_seen') or now_ts()))}\n\n"
        "اگه سالم فعالیت کنی، امتیازت میره بالا 😎"
    )


def _leaderboard_text(cid: int) -> str:
    rows = [dict(r) for r in db.conn.execute(
        "SELECT user_id,first_name,score,msg_count FROM user_profiles WHERE group_id=? ORDER BY score DESC,msg_count DESC LIMIT 10",
        (int(cid),),
    ).fetchall()]
    if not rows:
        return "هنوز آماری ندارم 😅\nچندتا پیام رد و بدل بشه، اینجا فعال‌ها رو نشون میدم."
    lines = ["🔥 فعال‌ترین‌های گروه", ""]
    for i, r in enumerate(rows, 1):
        name = r.get("first_name") or str(r.get("user_id"))
        lines.append(f"{i}. {name} — ⭐ {int(r.get('score') or 0)} | 💬 {int(r.get('msg_count') or 0)}")
    lines.append("\nدمتون گرم، گروه رو زنده نگه داشتین 😎")
    return "\n".join(lines)


def _recent_warning_text(cid: int, uid: int) -> str:
    data = db.get_warnings(int(cid), int(uid))
    lines = [f"⚠️ اخطارهای تو: {data.get('count',0)}"]
    reasons = data.get("reasons") or []
    if not reasons:
        lines.append("فعلاً اخطاری نداری؛ همین فرمون برو جلو 😎")
    else:
        for item in reasons[-5:]:
            lines.append(f"• {item.get('date','')} — {item.get('reason','')}")
    return "\n".join(lines)


def _appeal_url(event_id: int) -> Optional[str]:
    uname = get_bot_username()
    if not uname:
        return None
    return f"https://ble.ir/{uname}?start=appeal_{int(event_id)}"


def _appeal_keyboard(event_id: int) -> Optional[dict]:
    url = _appeal_url(event_id)
    if url:
        return inline_keyboard([[btn("🙋 اعتراض دارم", url=url)]])
    return inline_keyboard([[btn("🙋 اعتراض دارم", f"appeal:start:{int(event_id)}")]])


def _create_mod_event(cid: int, target: int, actor: Optional[int], event_type: str, reason: str, payload: Optional[dict] = None) -> int:
    with db._lock:
        cur = db.conn.execute(
            "INSERT INTO moderation_events(group_id,user_id,admin_id,event_type,reason,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            (int(cid), int(target), int(actor or 0), str(event_type), str(reason or ""), "active", now_ts(), json.dumps(payload or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def _get_mod_event(event_id: int) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM moderation_events WHERE id=?", (int(event_id),)).fetchone()
    return dict(row) if row else None


def _admin_notify_targets(cid: int) -> list[int | str]:
    targets: list[int | str] = []
    st = _settings(int(cid))
    if st.get("log_enabled") and st.get("log_chat_id"):
        targets.append(st.get("log_chat_id"))
    for oid in OWNER_IDS:
        if oid not in targets:
            targets.append(oid)
    try:
        for aid in list(get_group_admin_ids(cid))[:10]:
            if aid not in targets:
                targets.append(aid)
    except Exception:
        pass
    return targets


def _notify_admins(cid: int, text: str, keyboard: Optional[dict] = None) -> None:
    for target in _admin_notify_targets(cid):
        api.safe_send_message(target, text, reply_markup=keyboard, parse_mode="HTML")


def _create_report(msg: dict, reason: str = "") -> Optional[int]:
    cid = int(chat_id(msg)); reporter = user_id(msg_from(msg)) or 0
    reply = msg.get("reply_to_message") or {}
    if not reply:
        api.safe_send_message(cid, "روی پیام طرف ریپلای کن و بنویس «گزارش» تا بفرستم برای مدیرها 🚨", reply_to_message_id=message_id(msg))
        return None
    reported_user = msg_from(reply)
    reported_id = user_id(reported_user) or 0
    if reported_id and can_manage(cid, reported_id, "panel"):
        api.safe_send_message(cid, "گزارش روی پیام مدیرها ثبت نمی‌کنم 😅", reply_to_message_id=message_id(msg))
        return None
    with db._lock:
        cur = db.conn.execute(
            "INSERT INTO user_reports(group_id,reporter_id,reported_id,message_id,reason,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            (cid, reporter, reported_id, message_id(reply) or 0, reason or "گزارش کاربر", "pending", now_ts(), json.dumps({"reported_name": first_name(reported_user), "reporter_name": first_name(msg_from(msg)), "text": get_text(reply)[:500]}, ensure_ascii=False)),
        )
        rid = int(cur.lastrowid)
    _upsert_profile(cid, reporter, report_delta=1, score_delta=2)
    text = (
        f"🚨 گزارش جدید\n\n"
        f"👤 گزارش‌دهنده: {_h(first_name(msg_from(msg)))} | <code>{reporter}</code>\n"
        f"👥 کاربر گزارش‌شده: {_h(first_name(reported_user))} | <code>{reported_id}</code>\n"
        f"🏷 گروه: {_h(db.get_group_title(cid))}\n"
        f"📝 دلیل: {_h(reason or 'گزارش کاربر')}\n\n"
        f"متن پیام:\n{_h(get_text(reply)[:700] or 'بدون متن')}"
    )
    kb = inline_keyboard([[btn("✅ گزارش درسته", f"report:ok:{rid}"), btn("❌ رد گزارش", f"report:no:{rid}")]])
    _notify_admins(cid, text, kb)
    api.safe_send_message(cid, "گزارشت رفت برای مدیرها ✅\nاگه درست باشه، امتیاز مثبت هم می‌گیری 😎", reply_to_message_id=message_id(msg))
    return rid


def _smart_help_text(is_pm: bool = False) -> str:
    return (
        "🤖 راهنمای سریع\n\n"
        "چی لازم داری؟ از دکمه‌ها بزن؛ متن‌ها کوتاه و کاربردی‌اند 😄"
        if is_pm else
        "🤖 راهنمای گروه\n\n"
        "اینجا می‌تونی پروفایل، قوانین، اخطارها و گزارش تخلف رو راحت ببینی."
    )


def _smart_help_keyboard(group_id: Optional[int] = None, uid: Optional[int] = None) -> dict:
    if group_id:
        return inline_keyboard([
            [btn("👤 پروفایل من", f"uhelp:profile:{group_id}"), btn("⚠️ اخطارهای من", f"uhelp:warnings:{group_id}")],
            [btn("📜 قوانین گروه", f"uhelp:rules:{group_id}"), btn("🚨 چطور گزارش بدم؟", f"uhelp:report:{group_id}")],
            [btn("🔥 فعال‌های گروه", f"uhelp:leaderboard:{group_id}"), btn("💎 اشتراک چیه؟", f"uhelp:premium:{group_id}")],
        ])
    return private_main_keyboard(uid)


def custom_main_keyboard(gid: int) -> dict:  # override: clearer reset controls
    return inline_keyboard([
        [btn("🔇 متن عملیات‌ها", f"custom:texts:{gid}"), btn("🧹 متن قفل‌ها", f"custom:locks:{gid}")],
        [btn("⌨️ اسم دستورها", f"custom:cmds:{gid}"), btn("🎭 لحن آماده", f"custom:tones:{gid}")],
        [btn("👀 متغیرها و نمونه", f"custom:vars:{gid}")],
        [btn("🔄 ریست متن‌ها", f"custom:resettexts:{gid}"), btn("🔄 ریست دستورها", f"custom:resetcmds:{gid}")],
        [btn("🧹 ریست متن قفل‌ها", f"custom:resetlocktexts:{gid}"), btn("♻️ ریست همه", f"custom:resetall:{gid}")],
        [btn("⬅️ منوی اصلی", "pm:main")],
    ])


def _reset_custom_texts(gid: int, only_locks: bool = False) -> None:
    with db._lock:
        if only_locks:
            db.conn.execute("DELETE FROM custom_texts WHERE group_id=? AND (key LIKE 'violation_%' OR key IN ('lock_violation','lock_limit','lock_on','lock_off','all_locks_on','all_locks_off'))", (int(gid),))
        else:
            db.conn.execute("DELETE FROM custom_texts WHERE group_id=?", (int(gid),))


def _reset_custom_commands(gid: int) -> None:
    with db._lock:
        db.conn.execute("DELETE FROM custom_commands WHERE group_id=?", (int(gid),))


def warn_user_smart(cid: int, uid: int, reason: str, actor: Optional[int], public_name: str = "کاربر") -> str:
    _upsert_profile(int(cid), int(uid), score_delta=-20, risk_delta=20, warn_delta=1)
    return _USER_OLD_WARN_USER_SMART(cid, uid, reason, actor, public_name)


def moderate_message(msg: dict, settings: dict) -> bool:
    res = _USER_OLD_MODERATE_MESSAGE(msg, settings)
    if res:
        cid = int(chat_id(msg)); uid = user_id(msg_from(msg)) or 0
        if uid:
            _upsert_profile(cid, msg_from(msg), score_delta=-10, risk_delta=10, deleted_delta=1)
    return res


def handle_normal_group_message(msg: dict) -> None:
    cid = int(chat_id(msg)); user = msg_from(msg)
    uid = user_id(user)
    if uid and not can_manage(cid, uid, "panel"):
        _upsert_profile(cid, user, msg_delta=1, score_delta=1)
    return _USER_OLD_HANDLE_NORMAL_GROUP_MESSAGE(msg)


def get_command(text: str) -> Optional[Tuple[str, str]]:
    t = fa_norm(text or "")
    if t in {"پروفایل من", "امتیاز من", "سطح من"}:
        return "profile", "me"
    if t in {"پروفایل", "نمایش پروفایل"}:
        return "profile", ""
    if t in {"فعالها", "فعال ها", "فعال‌ها", "لیدربورد", "برترین ها", "برترین‌ها"}:
        return "leaderboard", ""
    if t in {"اخطارهای من", "اخطار ها من", "اخطارها من"}:
        return "mywarnings", ""
    if t == "گزارش" or t.startswith("گزارش "):
        return "report", t.replace("گزارش", "", 1).strip()
    if t in {"راهنمای هوشمند", "راهنمای سریع"}:
        return "smarthelp", ""
    m = _starts(t, ["ریست متن ها", "ریست متن‌ها", "ریست متن", "بازگردانی متن ها", "بازگردانی متن‌ها"])
    if m:
        return "reset_custom_texts", m[1]
    m = _starts(t, ["ریست دستورها", "ریست دستور ها", "بازگردانی دستورها"])
    if m:
        return "reset_custom_cmds", m[1]
    m = _starts(t, ["ریست شخصی سازی", "ریست شخصی‌سازی", "بازگردانی شخصی سازی", "بازگردانی شخصی‌سازی"])
    if m:
        return "reset_custom_all", m[1]
    return _USER_OLD_GET_COMMAND(text)


def handle_command(msg: dict, cmd: str, args: str) -> None:
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if cmd in {"smarthelp", "help"}:
        if is_group(msg):
            api.safe_send_message(cid, _smart_help_text(False), reply_to_message_id=message_id(msg), reply_markup=_smart_help_keyboard(int(cid), uid))
        else:
            api.safe_send_message(cid, _smart_help_text(True), reply_markup=_smart_help_keyboard(None, uid))
        return
    if cmd == "profile":
        if not is_group(msg):
            api.safe_send_message(cid, "پروفایل داخل گروه معنی داره 😅\nداخل گروه بزن: پروفایل من", reply_markup=private_main_keyboard(uid))
            return
        target = uid
        label = first_name(msg_from(msg))
        if (args or "") != "me" and (msg.get("reply_to_message") or {}) and can_manage(int(cid), uid, "stats"):
            ruser = msg_from(msg.get("reply_to_message") or {})
            target = user_id(ruser) or uid
            label = first_name(ruser)
            _upsert_profile(int(cid), ruser)
        api.safe_send_message(cid, _profile_text(int(cid), int(target), label), reply_to_message_id=message_id(msg))
        return
    if cmd == "leaderboard":
        if not is_group(msg):
            api.safe_send_message(cid, "این یکی برای داخل گروهه 😄")
            return
        api.safe_send_message(cid, _leaderboard_text(int(cid)), reply_to_message_id=message_id(msg))
        return
    if cmd == "mywarnings":
        if not is_group(msg):
            api.safe_send_message(cid, "اخطارها برای هر گروه جداست؛ داخل گروه بزن «اخطارهای من»")
            return
        api.safe_send_message(cid, _recent_warning_text(int(cid), int(uid or 0)), reply_to_message_id=message_id(msg))
        return
    if cmd == "report":
        if not is_group(msg):
            api.safe_send_message(cid, "برای گزارش تخلف، داخل گروه روی پیام طرف ریپلای کن و بنویس: گزارش")
            return
        _create_report(msg, args or "")
        return
    if cmd in {"reset_custom_texts", "reset_custom_cmds", "reset_custom_all"}:
        nums = re.findall(r"-?\d+", args or "")
        gid = int(nums[0]) if nums else (int(cid) if is_group(msg) else 0)
        if not gid:
            set_state(uid or 0, cmd, {})
            api.safe_send_message(cid, "آیدی عددی گروه رو بفرست تا ریست کنم.")
            return
        if is_group(msg):
            if not require_admin(msg, "panel"):
                return
            api.safe_send_message(cid, "ریست شخصی‌سازی فقط از پیوی انجام میشه 😅\nاز دکمه زیر برو پیوی ربات.", reply_markup=customization_entry_keyboard(gid))
            return
        if not _custom_pm_allowed(uid, gid):
            api.safe_send_message(cid, "برای این گروه دسترسی نداری 😕")
            return
        if cmd == "reset_custom_texts":
            _reset_custom_texts(gid); api.safe_send_message(cid, "همه متن‌های سفارشی برگشت به حالت اول ✅", reply_markup=custom_main_keyboard(gid)); return
        if cmd == "reset_custom_cmds":
            _reset_custom_commands(gid); api.safe_send_message(cid, "همه اسم دستورها برگشت به حالت اول ✅", reply_markup=custom_main_keyboard(gid)); return
        _reset_all_customization(gid); api.safe_send_message(cid, "کل شخصی‌سازی گروه ریست شد ✅", reply_markup=custom_main_keyboard(gid)); return
    return _USER_OLD_HANDLE_COMMAND(msg, cmd, args)


def handle_moderation(msg: dict, cmd: str, args: str) -> None:
    if not require_admin(msg, "warns"):
        return
    cid = int(chat_id(msg)); actor = user_id(msg_from(msg)); target, reason = get_target_from_reply_or_arg(msg, args)
    if not target:
        api.safe_send_message(cid, "روی پیام طرف ریپلای کن یا آیدیش رو بده.", reply_to_message_id=message_id(msg)); return
    if can_manage(cid, target, "panel"):
        api.safe_send_message(cid, "روی مدیرهای گروه کاری انجام نمیدم 😅", reply_to_message_id=message_id(msg)); return
    reason = reason or "بدون دلیل"
    ctx = _ctx_for_action(cid, msg, target, reason)
    try:
        if cmd == "ban":
            api.ban_chat_member(cid, target); db.log(cid, target, "ban", {"admin_id": actor, "reason": reason}); _upsert_profile(cid, target, score_delta=-50, risk_delta=40)
            api.safe_send_message(cid, _render_custom_template(cid, "ban_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "unban":
            api.unban_chat_member(cid, target, only_if_banned=True); db.log(cid, target, "unban", {"admin_id": actor}); _upsert_profile(cid, target, score_delta=10, risk_delta=-20)
            api.safe_send_message(cid, _render_custom_template(cid, "unban_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "kick":
            api.ban_chat_member(cid, target); time.sleep(0.3); api.unban_chat_member(cid, target, only_if_banned=False); db.log(cid, target, "kick", {"admin_id": actor, "reason": reason}); _upsert_profile(cid, target, score_delta=-30, risk_delta=25)
            api.safe_send_message(cid, _render_custom_template(cid, "kick_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "warn":
            event_id = _create_mod_event(cid, target, actor, "warn", reason)
            _upsert_profile(cid, target, score_delta=-20, risk_delta=20, warn_delta=1)
            text = _USER_OLD_WARN_USER_SMART(cid, target, reason, actor, _target_label_from_message(msg, target))
            api.safe_send_message(cid, text + "\n\nاگه فکر می‌کنی اشتباهه، از دکمه زیر اعتراض بزن.", reply_to_message_id=message_id(msg), reply_markup=_appeal_keyboard(event_id))
        elif cmd == "unwarn":
            db.clear_warnings(cid, target); _upsert_profile(cid, target, score_delta=15, risk_delta=-20)
            api.safe_send_message(cid, _render_custom_template(cid, "unwarn_success", ctx), reply_to_message_id=message_id(msg))
        elif cmd == "warnings":
            data = db.get_warnings(cid, target); lines = [f"⚠️ اخطارهای {_target_label_from_message(msg, target)}: {data['count']}"]
            for item in data["reasons"][-10:]: lines.append(f"• {item.get('date','')} — {item.get('reason','')}")
            api.safe_send_message(cid, "\n".join(lines), reply_to_message_id=message_id(msg))
        elif cmd == "mute":
            until_ts, rem = parse_duration(reason, default_seconds=3600); db.mute_user(cid, target, until_ts, rem or "میوت دستی")
            until_text = jalali_date(until_ts) if until_ts else "نامحدود"; ctx.update({"time": until_text, "until": until_text, "reason": rem or reason or "میوت دستی"})
            event_id = _create_mod_event(cid, target, actor, "mute", ctx.get("reason") or reason, {"until_ts": until_ts})
            _upsert_profile(cid, target, score_delta=-15, risk_delta=15)
            api.safe_send_message(cid, _render_custom_template(cid, "mute_success", ctx) + "\n\nاگه اشتباهی میوت شدی، اعتراض بزن.", reply_to_message_id=message_id(msg), reply_markup=_appeal_keyboard(event_id))
        elif cmd == "unmute":
            ok = db.unmute_user(cid, target); _upsert_profile(cid, target, score_delta=10, risk_delta=-15)
            api.safe_send_message(cid, _render_custom_template(cid, "unmute_success", ctx) if ok else "این کاربر میوت نبود.", reply_to_message_id=message_id(msg))
    except BaleAPIError as exc:
        api.safe_send_message(cid, f"انجام نشد 😕\n{exc.description}\nاحتمالاً دسترسی ادمین ربات کافی نیست.", reply_to_message_id=message_id(msg))


def handle_start(msg: dict, args: str) -> None:
    cid = chat_id(msg); uid = user_id(msg_from(msg))
    if is_private(msg):
        a = (args or "").strip()
        m = re.search(r"appeal_(\d+)", a)
        if m:
            event_id = int(m.group(1)); ev = _get_mod_event(event_id)
            if not ev:
                api.safe_send_message(cid, "این مورد اعتراض پیدا نشد 😕")
                return
            if int(ev.get("user_id") or 0) != int(uid or 0) and not is_bot_owner(uid):
                api.safe_send_message(cid, "این اعتراض برای تو نیست 😅")
                return
            set_state(uid or 0, "appeal_write", {"event_id": event_id})
            api.safe_send_message(cid, "باشه، توضیح بده چرا فکر می‌کنی اشتباه شده.\nمن همون رو برای مدیرها می‌فرستم بررسی کنن 🙋")
            return
    return _USER_OLD_HANDLE_START(msg, args)


def process_private_state(msg: dict, state_row: dict) -> bool:
    uid = user_id(msg_from(msg)); cid = chat_id(msg); text = get_text(msg).strip()
    state = state_row.get("state") if state_row else None
    data = state_row.get("data") or {}
    if state in {"reset_custom_texts", "reset_custom_cmds", "reset_custom_all"}:
        nums = re.findall(r"-?\d+", text)
        if not nums:
            api.safe_send_message(cid, "آیدی عددی گروه رو بفرست. مثال:\n<code>-100123456789</code>", parse_mode="HTML")
            return True
        gid = int(nums[0])
        clear_state(uid or 0)
        if not _custom_pm_allowed(uid, gid):
            api.safe_send_message(cid, "برای این گروه دسترسی نداری 😕")
            return True
        if state == "reset_custom_texts":
            _reset_custom_texts(gid); api.safe_send_message(cid, "متن‌ها ریست شد ✅", reply_markup=custom_main_keyboard(gid)); return True
        if state == "reset_custom_cmds":
            _reset_custom_commands(gid); api.safe_send_message(cid, "اسم دستورها ریست شد ✅", reply_markup=custom_main_keyboard(gid)); return True
        _reset_all_customization(gid); api.safe_send_message(cid, "همه شخصی‌سازی‌ها ریست شد ✅", reply_markup=custom_main_keyboard(gid)); return True
    if state == "appeal_write":
        if fa_norm(text) in {"لغو", "انصراف", "cancel"}:
            clear_state(uid or 0); api.safe_send_message(cid, "اعتراض لغو شد ✅", reply_markup=private_main_keyboard(uid)); return True
        event_id = int(data.get("event_id") or 0); ev = _get_mod_event(event_id)
        if not ev:
            clear_state(uid or 0); api.safe_send_message(cid, "این مورد دیگه پیدا نشد 😕", reply_markup=private_main_keyboard(uid)); return True
        if len(text) < 3:
            api.safe_send_message(cid, "یه توضیح کوتاه‌تر نه 😅\nمثلاً بنویس چرا فکر می‌کنی اشتباه شده.")
            return True
        with db._lock:
            cur = db.conn.execute(
                "INSERT INTO user_appeals(event_id,group_id,user_id,text,status,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, int(ev["group_id"]), int(uid or 0), text[:1200], "pending", now_ts()),
            )
            appeal_id = int(cur.lastrowid)
        _upsert_profile(int(ev["group_id"]), int(uid or 0), appeal_delta=1)
        kb = inline_keyboard([[btn("✅ قبول اعتراض", f"appeal:ok:{appeal_id}"), btn("❌ رد اعتراض", f"appeal:no:{appeal_id}")]])
        _notify_admins(int(ev["group_id"]), f"🙋 اعتراض جدید\n\n👤 کاربر: <code>{uid}</code>\n🏷 گروه: {_h(db.get_group_title(int(ev['group_id'])))}\n🔎 مورد: {_h(ev.get('event_type'))}\n📝 دلیل قبلی: {_h(ev.get('reason'))}\n\nتوضیح کاربر:\n{_h(text[:1200])}", kb)
        clear_state(uid or 0)
        api.safe_send_message(cid, "اعتراضت ثبت شد ✅\nمدیرها بررسی کنن، نتیجه رو همینجا بهت میگم.", reply_markup=private_main_keyboard(uid))
        return True
    return _USER_OLD_PROCESS_PRIVATE_STATE(msg, state_row)


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query")
    if not q:
        return False
    data = q.get("data") or ""; msg = q.get("message") or {}; cid = chat_id(msg); mid = message_id(msg); actor = user_id(q.get("from") or {})
    qid = q.get("id") or q.get("callback_query_id")
    def ans(text: str = "", alert: bool = False):
        if qid:
            api.answer_callback_query(qid, text, alert)

    if data.startswith("uhelp:"):
        ans()
        parts = data.split(":"); action = parts[1] if len(parts) > 1 else ""; gid = int(parts[2]) if len(parts) > 2 and re.fullmatch(r"-?\d+", parts[2]) else (int(cid) if cid and str(cid).lstrip('-').isdigit() else 0)
        if action == "profile":
            _edit_or_send_panel(cid, mid, _profile_text(gid, int(actor or 0), "تو"), inline_keyboard([[btn("⬅️ برگشت", f"uhelp:main:{gid}")]])); return True
        if action == "warnings":
            _edit_or_send_panel(cid, mid, _recent_warning_text(gid, int(actor or 0)), inline_keyboard([[btn("⬅️ برگشت", f"uhelp:main:{gid}")]])); return True
        if action == "rules":
            _edit_or_send_panel(cid, mid, "📜 قوانین گروه\n\n" + str(_settings(gid).get("rules_text") or "قوانین هنوز تنظیم نشده 😅"), inline_keyboard([[btn("⬅️ برگشت", f"uhelp:main:{gid}")]])); return True
        if action == "report":
            _edit_or_send_panel(cid, mid, "🚨 برای گزارش تخلف، روی پیام طرف ریپلای کن و بنویس:\n\n<code>گزارش</code>\n\nمن می‌فرستم برای مدیرها.", inline_keyboard([[btn("📋 کپی گزارش", copy_text="گزارش"), btn("⬅️ برگشت", f"uhelp:main:{gid}")]])); return True
        if action == "leaderboard":
            _edit_or_send_panel(cid, mid, _leaderboard_text(gid), inline_keyboard([[btn("⬅️ برگشت", f"uhelp:main:{gid}")]])); return True
        if action == "premium":
            _edit_or_send_panel(cid, mid, "💎 اشتراک یعنی امکانات حرفه‌ای گروه مثل قفل‌های کامل، آمار، ضدحمله، جوین اجباری و... فعال بمونه.\n\nخرید و تمدید از پیوی ربات انجام میشه.", inline_keyboard([[btn("🛒 خرید/تمدید", "buy:start:0"), btn("⬅️ برگشت", f"uhelp:main:{gid}")]])); return True
        _edit_or_send_panel(cid, mid, _smart_help_text(False), _smart_help_keyboard(gid, actor)); return True

    if data.startswith("report:"):
        parts = data.split(":"); action = parts[1] if len(parts) > 1 else ""; rid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        row = db.conn.execute("SELECT * FROM user_reports WHERE id=?", (rid,)).fetchone()
        if not row:
            ans("گزارش پیدا نشد 😕", True); return True
        row = dict(row); gid = int(row["group_id"])
        if not can_manage(gid, actor, "warns"):
            ans("این دکمه برای مدیرهاست 😅", True); return True
        ans()
        status = "confirmed" if action == "ok" else "rejected"
        with db._lock:
            db.conn.execute("UPDATE user_reports SET status=? WHERE id=?", (status, rid))
        if status == "confirmed":
            _upsert_profile(gid, int(row["reporter_id"]), score_delta=5, report_confirm_delta=1)
            if row.get("reported_id"):
                _upsert_profile(gid, int(row["reported_id"]), score_delta=-10, risk_delta=15)
            _edit_or_send_panel(cid, mid, "گزارش تایید شد ✅\nبه گزارش‌دهنده امتیاز مثبت دادم.", None)
        else:
            _edit_or_send_panel(cid, mid, "گزارش رد شد ✅", None)
        return True

    if data.startswith("appeal:"):
        parts = data.split(":"); action = parts[1] if len(parts) > 1 else ""; item_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if action == "start":
            ev = _get_mod_event(item_id)
            if not ev:
                ans("این مورد پیدا نشد 😕", True); return True
            if int(ev.get("user_id") or 0) != int(actor or 0):
                ans("این اعتراض برای تو نیست 😅", True); return True
            ans("بیا پیوی توضیحت رو بفرست", True)
            return True
        row = db.conn.execute("SELECT * FROM user_appeals WHERE id=?", (item_id,)).fetchone()
        if not row:
            ans("اعتراض پیدا نشد 😕", True); return True
        row = dict(row); gid = int(row["group_id"])
        if not can_manage(gid, actor, "warns"):
            ans("این دکمه برای مدیرهاست 😅", True); return True
        ans()
        new_status = "accepted" if action == "ok" else "rejected"
        with db._lock:
            db.conn.execute("UPDATE user_appeals SET status=?, handled_by=?, handled_at=? WHERE id=?", (new_status, int(actor or 0), now_ts(), item_id))
        ev = _get_mod_event(int(row["event_id"])) or {}
        target = int(row["user_id"])
        if new_status == "accepted":
            if ev.get("event_type") == "mute":
                db.unmute_user(gid, target)
            elif ev.get("event_type") == "warn":
                # یک راه امن: همه اخطارهای همان کاربر پاک شود؛ ساده و قابل فهم برای مدیر.
                db.clear_warnings(gid, target)
            _upsert_profile(gid, target, score_delta=20, risk_delta=-20)
            api.safe_send_message(target, f"اعتراضت قبول شد ✅\nمورد مربوط به گروه «{db.get_group_title(gid)}» اصلاح شد.")
            _edit_or_send_panel(cid, mid, "اعتراض قبول شد ✅\nبه کاربر هم خبر دادم.", None)
        else:
            api.safe_send_message(target, f"اعتراضت بررسی شد ولی قبول نشد 😕\nگروه: {db.get_group_title(gid)}")
            _edit_or_send_panel(cid, mid, "اعتراض رد شد ✅", None)
        return True

    if data.startswith("custom:"):
        # Additional PM reset shortcuts before previous customization callback handles the rest.
        parts = data.split(":"); action = parts[1] if len(parts) > 1 else ""; gid = 0
        if len(parts) > 2:
            try: gid = int(parts[2])
            except Exception: gid = 0
        if action in {"resettexts", "resetcmds", "resetlocktexts"} and gid:
            if not is_private(msg):
                ans("ریست فقط از پیوی انجام میشه 😅", True); return True
            if not _custom_pm_allowed(actor, gid):
                ans("دسترسی نداری 😅", True); return True
            ans()
            if action == "resettexts":
                _reset_custom_texts(gid); api.safe_send_message(cid, "همه متن‌های سفارشی برگشت به حالت اول ✅", reply_markup=custom_main_keyboard(gid)); return True
            if action == "resetcmds":
                _reset_custom_commands(gid); api.safe_send_message(cid, "همه اسم دستورها برگشت به حالت اول ✅", reply_markup=custom_main_keyboard(gid)); return True
            _reset_custom_texts(gid, only_locks=True); api.safe_send_message(cid, "متن قفل‌ها برگشت به حالت اول ✅", reply_markup=custom_main_keyboard(gid)); return True
    return _USER_OLD_HANDLE_CALLBACK(update)




# ============================================================
# Trusted/VIP users + smarter anti-ad pack
# - Trusted/VIP users can be managed from the group by admins.
# - Trusted/VIP users are exempt from noisy promotional/media/link locks.
# - Smart anti-ad catches ads without direct links, invite-to-PM, sale phrases, prices, VPN/config/member/follower ads.
# ============================================================

_TRUST_OLD_GET_COMMAND = get_command
_TRUST_OLD_HANDLE_COMMAND = handle_command
_TRUST_OLD_MODERATE_MESSAGE = moderate_message
_TRUST_OLD_HANDLE_NORMAL_GROUP_MESSAGE = handle_normal_group_message
_TRUST_OLD_PROFILE_TEXT = _profile_text
_TRUST_OLD_LEADERBOARD_TEXT = _leaderboard_text
_TRUST_OLD_SMART_HELP_TEXT = _smart_help_text
_TRUST_OLD_SMART_HELP_KEYBOARD = _smart_help_keyboard

TRUST_LEVELS = {
    "trusted": "🤝 کاربر معتمد",
    "vip": "👑 کاربر ویژه",
}

TRUST_EXEMPT_LOCKS = {
    "lock_links", "lock_bale_links", "lock_telegram_links", "lock_instagram_links", "lock_whatsapp_links", "lock_site_links",
    "lock_channels", "lock_mentions", "lock_usernames", "lock_phone_numbers", "lock_ads",
    "lock_media", "lock_photos", "lock_videos", "lock_voice", "lock_audio", "lock_files", "lock_stickers", "lock_gifs",
    "lock_contacts", "lock_locations", "lock_forwards", "lock_duplicate_messages", "lock_short_text", "lock_long_text",
}

SMART_AD_SALE_WORDS = {
    "خرید", "فروش", "میفروشم", "می فروشم", "بفروش", "سفارش", "ثبت سفارش", "قیمت", "تعرفه", "پلن",
    "ارزان", "ارزون", "تخفیف", "حراج", "درآمد", "کسب درآمد", "سرمایه گذاری", "سرمایه گذاری", "تضمینی",
    "شارژ", "پرداخت", "واریز", "کارت", "کارت به کارت", "رسید", "اکانت", "اشتراک",
}
SMART_AD_CONTACT_WORDS = {
    "پیوی", "خصوصی", "دایرکت", "پیام بده", "پیام بدید", "پیام بدهید", "زنگ بزن", "تماس بگیر", "تماس بگیرید",
    "ارتباط", "ادمین", "پشتیبانی", "درخدمتم", "در خدمتم", "لینک بیو", "بیا پیوی", "بیاین پیوی",
}
SMART_AD_PRODUCT_WORDS = {
    "ممبر", "فالوور", "لایک", "ویو", "بازدید", "کانال", "چنل", "گروه", "پیج", "اینستا", "تلگرام", "بله",
    "vpn", "وی پی ان", "فیلترشکن", "کانفیگ", "پروکسی", "سرور", "هاست", "دامنه", "اکانت پرمیوم", "پریمیوم",
    "ربات", "سورس", "تبلیغ", "تبلیغات", "آگهی", "اگهی", "رپورتاژ", "عضوگیری", "زیرمجموعه",
}
PRICE_RE_SMART = re.compile(r"(?i)(\d{1,3}(?:[,.،]?\d{3})+|\d+\s*(?:تومن|تومان|ریال|هزار|میلیون)|رایگان|free|قیمت\s*[:：])")
CARD_RE_SMART = re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)")
INVITE_RE_SMART = re.compile(r"(?i)(عضو\s*شو|جوین\s*شو|join\s*(?:us|now)?|subscribe|سابسکرایب|فالو\s*کن|دنبال\s*کن)")


def ensure_trusted_schema() -> None:
    with db._lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS trusted_users (
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            level TEXT NOT NULL DEFAULT 'trusted',
            title TEXT,
            added_by INTEGER,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(group_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_trusted_users_group_level ON trusted_users(group_id, level);
        """)


ensure_trusted_schema()


def _trust_row(cid: int, uid: int) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM trusted_users WHERE group_id=? AND user_id=?", (int(cid), int(uid))).fetchone()
    return dict(row) if row else None


def _trust_level(cid: int, uid: int) -> str:
    row = _trust_row(cid, uid)
    return str(row.get("level") or "") if row else ""


def _is_trusted_user(cid: int, uid: int) -> bool:
    return _trust_level(cid, uid) in {"trusted", "vip"}


def _is_vip_user(cid: int, uid: int) -> bool:
    return _trust_level(cid, uid) == "vip"


def _set_trust_level(cid: int, uid: int, level: str, added_by: Optional[int] = None, title: str = "") -> None:
    level = "vip" if level == "vip" else "trusted"
    with db._lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO trusted_users(group_id,user_id,level,title,added_by,created_at) VALUES(?,?,?,?,?,?)",
            (int(cid), int(uid), level, title or TRUST_LEVELS[level], int(added_by or 0), now_ts()),
        )
    try:
        if level == "vip":
            _upsert_profile(cid, uid, score_delta=80, risk_delta=-50)
        else:
            _upsert_profile(cid, uid, score_delta=40, risk_delta=-30)
    except Exception:
        pass


def _remove_trust_level(cid: int, uid: int, mode: str = "all") -> None:
    with db._lock:
        if mode == "vip":
            row = _trust_row(cid, uid)
            if row and row.get("level") == "vip":
                db.conn.execute("UPDATE trusted_users SET level=?, title=? WHERE group_id=? AND user_id=?", ("trusted", TRUST_LEVELS["trusted"], int(cid), int(uid)))
        else:
            db.conn.execute("DELETE FROM trusted_users WHERE group_id=? AND user_id=?", (int(cid), int(uid)))


def _list_trust_users(cid: int, level: Optional[str] = None) -> str:
    if level:
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM trusted_users WHERE group_id=? AND level=? ORDER BY created_at DESC LIMIT 50", (int(cid), level)).fetchall()]
    else:
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM trusted_users WHERE group_id=? ORDER BY level DESC, created_at DESC LIMIT 50", (int(cid),)).fetchall()]
    if not rows:
        return "هنوز کسی توی این لیست نیست 😅"
    title = "👑 کاربرهای ویژه" if level == "vip" else ("🤝 کاربرهای معتمد" if level == "trusted" else "🤝👑 معتمدها و ویژه‌ها")
    lines = [title, ""]
    for i, r in enumerate(rows, 1):
        uid = int(r.get("user_id") or 0)
        lvl = TRUST_LEVELS.get(r.get("level"), r.get("level"))
        prof = _profile_row(int(cid), uid)
        name = prof.get("first_name") or str(uid)
        lines.append(f"{i}. {lvl} — {name} | <code>{uid}</code>")
    return "\n".join(lines)


def _target_user_label(cid: int, uid: int, msg: Optional[dict] = None) -> str:
    if msg:
        lbl = _target_label_from_message(msg, uid)
        if lbl and lbl != str(uid):
            return lbl
    prof = _profile_row(int(cid), int(uid))
    return prof.get("first_name") or str(uid)


def _smart_ad_reason(text: str) -> Optional[str]:
    raw = text or ""
    t = fa_norm(raw).lower()
    if len(t) < 4:
        return None
    score = 0
    tags: list[str] = []

    def has_any(words: set[str]) -> list[str]:
        found = []
        for w in words:
            wn = fa_norm(w).lower()
            if wn and wn in t:
                found.append(w)
        return found

    sale = has_any(SMART_AD_SALE_WORDS)
    contact = has_any(SMART_AD_CONTACT_WORDS)
    product = has_any(SMART_AD_PRODUCT_WORDS)
    has_link = bool(LINK_RE.search(raw) or BALE_LINK_RE.search(raw) or TELEGRAM_LINK_RE.search(raw) or SITE_LINK_RE.search(raw))
    has_user = bool(MENTION_RE.search(raw) or CHANNEL_RE.search(raw))
    has_phone = bool(PHONE_RE.search(normalize_digits(raw)))
    has_price = bool(PRICE_RE_SMART.search(normalize_digits(raw)))
    has_card = bool(CARD_RE_SMART.search(normalize_digits(raw)))
    has_invite = bool(INVITE_RE_SMART.search(raw))

    if sale:
        score += 1; tags.append("فروش/قیمت")
    if contact:
        score += 1; tags.append("دعوت به پیوی/تماس")
    if product:
        score += 1; tags.append("کلمه تبلیغاتی")
    if has_link or has_user or has_phone:
        score += 1; tags.append("راه ارتباطی")
    if has_price:
        score += 1; tags.append("قیمت/مبلغ")
    if has_card:
        score += 2; tags.append("شماره کارت/پرداخت")
    if has_invite:
        score += 2; tags.append("دعوت به عضویت")

    # تبلیغ‌های رایج بدون لینک: «برای خرید بیا پیوی»، «کانفیگ ارزون دارم»، «ممبر تضمینی» و...
    if score >= 2:
        return "تبلیغ هوشمند" + (f" ({'، '.join(dict.fromkeys(tags[:4]))})" if tags else "")
    return None


def _handle_smart_ad_violation(msg: dict, reason: str, settings: dict) -> bool:
    cid = int(chat_id(msg)); uid = user_id(msg_from(msg)) or 0; mid = message_id(msg)
    if not uid or can_manage(cid, uid, "panel"):
        return False
    api.safe_delete_message(cid, mid)
    try:
        ext_record_daily(cid, uid, deleted_delta=1)
        _upsert_profile(cid, msg_from(msg), score_delta=-12, risk_delta=18, deleted_delta=1)
    except Exception:
        pass
    count = db.warn_user(cid, uid, reason, None)
    try:
        _upsert_profile(cid, uid, warn_delta=1)
    except Exception:
        pass
    limit = int(settings.get("warn_limit", 3))
    send_group_log(cid, "تبلیغ هوشمند حذف شد", f"برای: <code>{uid}</code>\nدلیل: {reason}\nوضعیت اخطار: {count}/{limit}")
    ctx = {"user": first_name(msg_from(msg)), "user_id": uid, "reason": reason, "warns": count, "limit": limit, "group": db.get_group_title(int(cid)), "date": jalali_date()}
    if count >= limit:
        result = _apply_warn_punishment(cid, uid, reason, None)
        ctx["action"] = result
        api.safe_send_message(cid, _render_custom_template(cid, "lock_limit", ctx))
    else:
        api.safe_send_message(cid, _render_custom_template(cid, "violation_lock_ads", ctx, _custom_get_text(cid, "lock_violation", CUSTOM_TEXT_DEFS["lock_violation"][1])))
    return True


def get_command(text: str) -> Optional[Tuple[str, str]]:
    t = fa_norm(text or "")
    starts_map = [
        (["معتمد کن", "معتمد کردن", "اعتماد بده", "اعتماد دادن", "مجاز کن"], "trust_user"),
        (["ویژه کن", "عضو ویژه کن", "ویژه کردن", "vip کن", "وی ای پی کن"], "vip_user"),
        (["حذف معتمد", "برداشتن معتمد", "لغو معتمد", "حذف اعتماد"], "untrust_user"),
        (["حذف ویژه", "برداشتن ویژه", "لغو ویژه", "حذف وی آی پی", "حذف vip"], "unvip_user"),
    ]
    for phrases, cmd in starts_map:
        m = _starts(t, phrases)
        if m:
            return cmd, m[1]
    if t in {"معتمدها", "معتمد ها", "لیست معتمدها", "کاربران معتمد", "کاربرهای معتمد"}:
        return "trusted_list", "trusted"
    if t in {"ویژه ها", "ویژه‌ها", "لیست ویژه ها", "لیست ویژه‌ها", "کاربران ویژه", "کاربرهای ویژه", "vip ها"}:
        return "trusted_list", "vip"
    if t in {"معتمد و ویژه", "معتمدها و ویژه‌ها", "لیست معتمد و ویژه"}:
        return "trusted_list", ""
    if t in {"ضد تبلیغ هوشمند روشن", "ضدتبلیغ هوشمند روشن", "تبلیغ هوشمند روشن", "ضد تبلیغ پیشرفته روشن"}:
        return "smart_ads_on", ""
    if t in {"ضد تبلیغ هوشمند خاموش", "ضدتبلیغ هوشمند خاموش", "تبلیغ هوشمند خاموش", "ضد تبلیغ پیشرفته خاموش"}:
        return "smart_ads_off", ""
    if t in {"وضعیت ضد تبلیغ", "وضعیت ضدتبلیغ", "ضد تبلیغ"}:
        return "smart_ads_status", ""
    return _TRUST_OLD_GET_COMMAND(text)


def handle_command(msg: dict, cmd: str, args: str) -> None:
    cid = chat_id(msg); actor = user_id(msg_from(msg))
    if cmd in {"trust_user", "vip_user", "untrust_user", "unvip_user"}:
        if not is_group(msg):
            api.safe_send_message(cid, "این دستور باید داخل گروه زده بشه 😄")
            return
        if not require_admin(msg, "roles"):
            return
        gid = int(cid)
        target, _reason = get_target_from_reply_or_arg(msg, args or "")
        if not target:
            api.safe_send_message(cid, "روی پیام طرف ریپلای کن یا آیدیش رو بده.", reply_to_message_id=message_id(msg))
            return
        name = _target_user_label(gid, int(target), msg)
        if cmd == "trust_user":
            _set_trust_level(gid, int(target), "trusted", actor)
            api.safe_send_message(cid, f"اوکی ✅\n{name} شد کاربر معتمد 🤝\nاز این به بعد روی قفل‌های تبلیغاتی/لینک/رسانه کمتر اذیتش می‌کنم.", reply_to_message_id=message_id(msg))
            return
        if cmd == "vip_user":
            _set_trust_level(gid, int(target), "vip", actor)
            api.safe_send_message(cid, f"تبریک 😎\n{name} شد کاربر ویژه 👑\nتوی پروفایلش هم نشون میدم.", reply_to_message_id=message_id(msg))
            return
        if cmd == "unvip_user":
            _remove_trust_level(gid, int(target), "vip")
            api.safe_send_message(cid, f"اوکی ✅\nویژه بودن {name} برداشته شد. اگه معتمد بوده، معتمد می‌مونه.", reply_to_message_id=message_id(msg))
            return
        _remove_trust_level(gid, int(target), "all")
        api.safe_send_message(cid, f"اوکی ✅\n{name} از لیست معتمد/ویژه برداشته شد.", reply_to_message_id=message_id(msg))
        return

    if cmd == "trusted_list":
        if not is_group(msg):
            api.safe_send_message(cid, "لیست معتمدها برای هر گروه جداست؛ داخل گروه بزن معتمدها.")
            return
        if not require_admin(msg, "stats"):
            return
        level = args if args in {"trusted", "vip"} else None
        api.safe_send_message(cid, _list_trust_users(int(cid), level), parse_mode="HTML", reply_to_message_id=message_id(msg))
        return

    if cmd in {"smart_ads_on", "smart_ads_off", "smart_ads_status"}:
        if not is_group(msg):
            api.safe_send_message(cid, "تنظیم ضدتبلیغ برای داخل گروهه 😄")
            return
        if not require_admin(msg, "locks"):
            return
        gid = int(cid)
        if cmd == "smart_ads_on":
            db.update_group_setting(gid, "lock_ads", True)
            db.update_group_setting(gid, "smart_ads_enabled", True)
            api.safe_send_message(cid, "ضدتبلیغ هوشمند روشن شد 🛡\nاز این به بعد تبلیغ‌های بدون لینک مثل «بیا پیوی»، «کانفیگ ارزون»، «ممبر تضمینی» رو هم بهتر می‌گیرم.", reply_to_message_id=message_id(msg))
            return
        if cmd == "smart_ads_off":
            db.update_group_setting(gid, "smart_ads_enabled", False)
            api.safe_send_message(cid, "ضدتبلیغ هوشمند خاموش شد ✅\nقفل تبلیغ ساده اگه روشن باشه، هنوز طبق تنظیم خودش کار می‌کنه.", reply_to_message_id=message_id(msg))
            return
        st = _settings(gid)
        api.safe_send_message(cid, f"🛡 وضعیت ضدتبلیغ\n\nقفل تبلیغ: {'روشن ✅' if st.get('lock_ads') else 'خاموش ❌'}\nضدتبلیغ هوشمند: {'روشن ✅' if st.get('smart_ads_enabled', True) else 'خاموش ❌'}", reply_to_message_id=message_id(msg))
        return

    return _TRUST_OLD_HANDLE_COMMAND(msg, cmd, args)


def _profile_text(cid: int, uid: int, display_name: str = "") -> str:
    base = _TRUST_OLD_PROFILE_TEXT(cid, uid, display_name)
    level = _trust_level(int(cid), int(uid))
    if not level:
        return base
    badge = TRUST_LEVELS.get(level, level)
    extra = f"\n🏷 وضعیت ویژه: {badge}"
    if level == "trusted":
        extra += "\n🛡 معاف از قفل‌های تبلیغاتی/لینک/رسانه"
    elif level == "vip":
        extra += "\n👑 عضو ویژه و معتمد گروه"
    return base.replace("\n\n", extra + "\n\n", 1)


def _leaderboard_text(cid: int) -> str:
    txt = _TRUST_OLD_LEADERBOARD_TEXT(cid)
    try:
        vip_rows = [dict(r) for r in db.conn.execute("SELECT user_id FROM trusted_users WHERE group_id=? AND level='vip' ORDER BY created_at DESC LIMIT 3", (int(cid),)).fetchall()]
        if vip_rows:
            names = []
            for r in vip_rows:
                prof = _profile_row(int(cid), int(r["user_id"]))
                names.append(prof.get("first_name") or str(r["user_id"]))
            txt += "\n\n👑 ویژه‌های گروه: " + "، ".join(names)
    except Exception:
        pass
    return txt


def _smart_help_text(is_pm: bool = False) -> str:
    base = _TRUST_OLD_SMART_HELP_TEXT(is_pm)
    if is_pm:
        return base
    return base + "\n\nدستورهای خوب برای اعضا:\n<code>پروفایل من</code> | <code>فعال‌ها</code> | <code>اخطارهای من</code>"


def moderate_message(msg: dict, settings: dict) -> bool:
    cid = int(chat_id(msg)); uid = user_id(msg_from(msg)) or 0
    if uid and _is_trusted_user(cid, uid):
        # معتمدها/VIPها از قفل‌های تبلیغاتی و قفل‌های پرخطا معاف‌اند، ولی فحش، فلود، قفل چت، شبانه و میوت همچنان اعمال می‌شود.
        patched = dict(settings or {})
        for key in TRUST_EXEMPT_LOCKS:
            patched[key] = False
        return _TRUST_OLD_MODERATE_MESSAGE(msg, patched)

    text = get_text(msg) or ""
    if settings.get("lock_ads") and settings.get("smart_ads_enabled", True):
        reason = _smart_ad_reason(text)
        if reason:
            return _handle_smart_ad_violation(msg, reason, settings)
    return _TRUST_OLD_MODERATE_MESSAGE(msg, settings)


def handle_normal_group_message(msg: dict) -> None:
    cid = int(chat_id(msg)); uid = user_id(msg_from(msg)) or 0
    try:
        if uid and _is_vip_user(cid, uid) and not can_manage(cid, uid, "panel"):
            _upsert_profile(cid, msg_from(msg), score_delta=1)  # VIPها برای فعالیت سالم کمی امتیاز بیشتر می‌گیرند.
    except Exception:
        pass
    return _TRUST_OLD_HANDLE_NORMAL_GROUP_MESSAGE(msg)


# ============================================================
# Install flow + cleaner premium group panel
# - When bot is added to a group, it asks admins to run: نصب
# - The install command initializes the group and shows a clean panel
# - Main panel text and buttons are redesigned to be clearer and tidier
# ============================================================
_INSTALL_UI_OLD_SET_BOT_USERNAME = set_bot_username_from_me
_INSTALL_UI_OLD_GET_COMMAND = get_command
_INSTALL_UI_OLD_HANDLE_COMMAND = handle_command
_INSTALL_UI_OLD_HANDLE_CALLBACK = handle_callback
_INSTALL_UI_OLD_PROCESS_UPDATE = process_update
_INSTALL_UI_OLD_HANDLE_PANEL = handle_panel
_INSTALL_UI_OLD_PANEL_TEXT = _panel_text
_INSTALL_UI_OLD_SECTION_KEYBOARD = _section_keyboard
_INSTALL_UI_OLD_FORMAT_MAIN_PANEL = _format_main_panel
_INSTALL_UI_OLD_PANEL_KEYBOARD = panel_keyboard

_BOT_ME_CACHE: dict = {}


def set_bot_username_from_me(me: dict) -> None:
    """Keep previous username behavior and also cache bot id for join detection."""
    try:
        _INSTALL_UI_OLD_SET_BOT_USERNAME(me)
    except Exception:
        pass
    try:
        bid = user_id(me or {})
        if bid:
            db.set_meta("bot_id", bid)
            _BOT_ME_CACHE["id"] = int(bid)
        uname = (me or {}).get("username") or (me or {}).get("user_name") or ""
        if uname:
            _BOT_ME_CACHE["username"] = str(uname).lstrip("@")
    except Exception:
        pass


def _get_bot_id_cached() -> Optional[int]:
    if _BOT_ME_CACHE.get("id"):
        return int(_BOT_ME_CACHE["id"])
    mid = db.get_meta("bot_id")
    if mid and str(mid).isdigit():
        _BOT_ME_CACHE["id"] = int(mid)
        return int(mid)
    try:
        me = api.get_me()
        set_bot_username_from_me(me)
        return int(_BOT_ME_CACHE.get("id") or 0) or None
    except Exception:
        return None


def _installed_key(cid: int | str) -> str:
    return f"group_installed:{cid}"


def _prompted_key(cid: int | str) -> str:
    return f"install_prompted:{cid}"


def _is_group_installed(cid: int | str) -> bool:
    return db.get_meta(_installed_key(cid), "0") not in {None, "", "0", "false", "False"}


def _mark_group_installed(cid: int | str) -> None:
    db.set_meta(_installed_key(cid), now_ts())


def _bot_is_admin_text(cid: int | str) -> str:
    bot_id_ = _get_bot_id_cached()
    if not bot_id_:
        return "نامشخص ⚪"
    try:
        admins = get_group_admin_ids(cid)
        return "ادمینه ✅" if int(bot_id_) in admins else "ادمین نیست ❌"
    except Exception:
        return "نامشخص ⚪"


def _install_prompt_keyboard(cid: Optional[int] = None) -> dict:
    rows = [
        [btn("📋 کپی دستور نصب", copy_text="نصب"), btn("📋 کپی پنل", copy_text="پنل")],
        [btn("📖 راهنمای سریع", copy_text="راهنما")],
    ]
    if cid is not None:
        rows.insert(0, [btn("✅ نصب / بررسی دسترسی", "install:check")])
    return inline_keyboard(rows)


def _install_prompt_text(title: str = "این گروه") -> str:
    return (
        "سلام، من اضافه شدم 👋\n\n"
        f"برای اینکه مدیریت {title} رو شروع کنم، لطفاً یکی از مدیرهای گروه این دستور رو بفرسته:\n\n"
        "<code>نصب</code>\n\n"
        "قبلش بهتره منو ادمین کنی و دسترسی حذف پیام، بن/اخراج و مدیریت پیام‌ها رو بدی تا قفل‌ها درست کار کنن."
    )


def _install_success_text(cid: int) -> str:
    title = db.get_group_title(int(cid)) or str(cid)
    st = _settings(int(cid))
    enabled_locks = sum(1 for k, v in (st or {}).items() if str(k).startswith("lock_") and bool(v))
    return (
        "✅ نصب انجام شد\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 گروه: {title}\n"
        f"🤖 دسترسی ربات: {_bot_is_admin_text(cid)}\n"
        f"🔒 قفل‌های روشن: {enabled_locks}\n"
        f"🛡 ضدفلود: {'روشن ✅' if st.get('anti_flood_enabled') else 'خاموش ❌'}\n\n"
        "از این به بعد برای تنظیمات فقط بزن:\n"
        "<code>پنل</code>\n\n"
        "پنل جدید تمیزتره؛ همه بخش‌ها دسته‌بندی شدن و دکمه‌ها واضح‌ترن."
    )


def _run_install_for_group(msg: dict) -> None:
    cid = int(chat_id(msg))
    db.ensure_group(cid, msg_chat(msg).get("title") or "")
    _mark_group_installed(cid)
    db.set_meta(_prompted_key(cid), now_ts())
    api.safe_send_message(
        cid,
        _install_success_text(cid),
        reply_to_message_id=message_id(msg),
        parse_mode="HTML",
        reply_markup=panel_keyboard(cid),
    )


def _maybe_handle_bot_added_update(update: dict) -> bool:
    """Detect Telegram/Bale-like service updates where this bot joins a group."""
    # my_chat_member style update
    mcm = update.get("my_chat_member") or update.get("chat_member") or {}
    if mcm:
        chat = mcm.get("chat") or {}
        ctype = (chat.get("type") or "").lower()
        cid = chat.get("id")
        new_cm = mcm.get("new_chat_member") or {}
        new_status = (new_cm.get("status") or "").lower()
        new_user = new_cm.get("user") or {}
        bot_id_ = _get_bot_id_cached()
        is_this_bot = bool(bot_id_ and user_id(new_user) == bot_id_)
        if cid and (ctype in {"group", "supergroup", "channel"} or str(cid).startswith("-")) and is_this_bot and new_status in {"member", "administrator", "creator"}:
            db.ensure_group(int(cid), chat.get("title") or "")
            if not _is_group_installed(int(cid)):
                api.safe_send_message(int(cid), _install_prompt_text(chat.get("title") or "این گروه"), parse_mode="HTML", reply_markup=_install_prompt_keyboard(int(cid)))
                db.set_meta(_prompted_key(int(cid)), now_ts())
            return True

    # service message style update
    msg = get_message(update)
    if not msg or not is_group(msg):
        return False
    cid = chat_id(msg)
    new_members = msg.get("new_chat_members") or []
    if not new_members:
        return False
    bot_id_ = _get_bot_id_cached()
    bot_uname = get_bot_username()
    added_me = False
    for member in new_members:
        mid = user_id(member)
        muname = str((member or {}).get("username") or (member or {}).get("user_name") or "").lstrip("@")
        if bot_id_ and mid == bot_id_:
            added_me = True
        elif bot_uname and muname and muname.lower() == bot_uname.lower():
            added_me = True
        elif (member or {}).get("is_bot") and len(new_members) == 1 and not bot_id_:
            # Last-resort fallback when getMe is unavailable; avoid matching multiple bot joins.
            added_me = True
    if not added_me:
        return False
    db.ensure_group(int(cid), msg_chat(msg).get("title") or "")
    if not _is_group_installed(int(cid)):
        prompted = db.get_meta(_prompted_key(int(cid)))
        # Avoid duplicate prompts from replayed service updates.
        if not prompted or now_ts() - int(prompted or 0) > 300:
            api.safe_send_message(int(cid), _install_prompt_text(msg_chat(msg).get("title") or "این گروه"), parse_mode="HTML", reply_markup=_install_prompt_keyboard(int(cid)))
            db.set_meta(_prompted_key(int(cid)), now_ts())
    return True


def get_command(text: str) -> Optional[Tuple[str, str]]:
    t = fa_norm(text or "")
    if t in {"نصب", "راه اندازی", "راه‌اندازی", "فعال سازی", "فعال‌سازی", "نصب ربات", "راه اندازی ربات"}:
        return "install", ""
    return _INSTALL_UI_OLD_GET_COMMAND(text)


def _status_line(value: bool) -> str:
    return "روشن ✅" if value else "خاموش ❌"


def _format_main_panel(cid: Optional[int]) -> str:
    title = db.get_group_title(int(cid)) if cid else "چت خصوصی"
    st = _settings(int(cid)) if cid and str(cid).lstrip("-").isdigit() else {}
    sub = _subscription_status(int(cid)) if cid and str(cid).lstrip("-").isdigit() else {"active": True}
    sub_status = "فعال ✅" if sub.get("active") else "غیرفعال ❌"
    installed = _is_group_installed(int(cid)) if cid and str(cid).lstrip("-").isdigit() else True
    enabled_locks = sum(1 for k, v in (st or {}).items() if str(k).startswith("lock_") and bool(v))
    bot_access = _bot_is_admin_text(int(cid)) if cid and str(cid).lstrip("-").isdigit() else "-"
    return (
        "╭─ 🎛 پنل مدیریت گروه\n"
        "├━━━━━━━━━━━━━━\n"
        f"├ 👥 گروه: {title}\n"
        f"├ ✅ نصب: {'انجام شده ✅' if installed else 'انجام نشده ⚠️'}\n"
        f"├ 🤖 دسترسی ربات: {bot_access}\n"
        f"├ 🔒 قفل‌های روشن: {enabled_locks}\n"
        f"├ 🛡 ضدفلود: {_status_line(bool(st.get('anti_flood_enabled')))}\n"
        f"├ 💎 اشتراک: {sub_status}\n"
        "╰━━━━━━━━━━━━━━\n\n"
        "از دکمه‌های پایین بخش موردنظرت رو انتخاب کن؛ همه چی دسته‌بندی و تمیز شده 👇"
    )


def panel_keyboard(group_id: Optional[int] = None) -> dict:
    return inline_keyboard([
        [btn("✅ نصب / بررسی", "install:check")],
        [btn("🔒 قفل‌ها", "locks:core"), btn("🛡 ضدتبلیغ", "p:smartads")],
        [btn("⚠️ اخطار و میوت", "p:warn"), btn("🚨 ضدحمله", "p:attack")],
        [btn("👋 خوشامد و قوانین", "p:welcome"), btn("📢 جوین اجباری", "p:force")],
        [btn("👥 کاربران", "p:users"), btn("📊 آمار", "p:stats")],
        [btn("👮 مدیرها", "p:roles"), btn("🎨 شخصی‌سازی", "custom:group")],
        [btn("💎 اشتراک", "p:premium"), btn("💳 پرداخت", "p:payment")],
        [btn("💾 بکاپ", "p:backup"), btn("📖 راهنمای سریع", "p:quick")],
    ])


def _panel_text(section: str, cid: int) -> str:
    st = _settings(cid)
    if section == "quick":
        return (
            "📖 راهنمای سریع\n"
            "━━━━━━━━━━━━━━\n\n"
            "دستورهای مهم:\n"
            "• <code>نصب</code> — راه‌اندازی ربات\n"
            "• <code>پنل</code> — باز کردن پنل\n"
            "• <code>قفل لینک</code> / <code>باز کردن لینک</code>\n"
            "• <code>میوت ۱۰ دقیقه اسپم</code>\n"
            "• <code>پروفایل من</code>\n"
            "• <code>فعال‌ها</code>\n"
            "• <code>معتمد کن</code> با ریپلای\n\n"
            "هر چیزی رو هم خواستی از پنل دکمه‌ای تنظیم کن."
        )
    if section == "users":
        return (
            "👥 بخش کاربران\n"
            "━━━━━━━━━━━━━━\n\n"
            "اینجا ابزارهای مربوط به اعضاست:\n"
            "• پروفایل و امتیاز\n"
            "• فعال‌ترین‌ها\n"
            "• معتمد / ویژه کردن\n"
            "• گزارش تخلف\n"
            "• اخطارهای کاربر\n\n"
            "برای معتمد/ویژه کردن، روی پیام کاربر ریپلای کن و دستور رو بفرست."
        )
    if section == "smartads":
        return (
            "🛡 ضدتبلیغ هوشمند\n"
            "━━━━━━━━━━━━━━\n\n"
            f"قفل تبلیغ: {_status_line(bool(st.get('lock_ads')))}\n"
            f"تشخیص هوشمند: {_status_line(bool(st.get('smart_ads_enabled', True)))}\n\n"
            "این بخش فقط لینک رو نمی‌گیره؛ پیام‌هایی مثل «بیا پیوی»، «فروش کانفیگ»، «ممبر تضمینی» و تبلیغ‌های بدون لینک رو هم بررسی می‌کنه."
        )
    if section == "install":
        return _install_success_text(cid) if _is_group_installed(cid) else _install_prompt_text(db.get_group_title(cid) or "این گروه")
    return _INSTALL_UI_OLD_PANEL_TEXT(section, cid)


def _section_keyboard(section: str, cid: int) -> dict:
    if section == "quick":
        return inline_keyboard([
            [btn("📋 کپی نصب", copy_text="نصب"), btn("📋 کپی پنل", copy_text="پنل")],
            [btn("🔒 قفل لینک", copy_text="قفل لینک"), btn("🔓 باز لینک", copy_text="باز کردن لینک")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "users":
        return inline_keyboard([
            [btn("👤 پروفایل من", copy_text="پروفایل من"), btn("🔥 فعال‌ها", copy_text="فعال‌ها")],
            [btn("🤝 معتمد کن", copy_text="معتمد کن"), btn("👑 ویژه کن", copy_text="ویژه کن")],
            [btn("📋 معتمدها", copy_text="معتمدها"), btn("🚨 گزارش", copy_text="گزارش")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "smartads":
        st = _settings(cid)
        return inline_keyboard([
            [btn("🛡 روشن کن", "smartads:on"), btn("❌ خاموش کن", "smartads:off")],
            [btn("🔒 قفل‌های تبلیغات", "locks:ads"), btn("📋 وضعیت", copy_text="وضعیت ضد تبلیغ")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    if section == "install":
        return inline_keyboard([
            [btn("✅ نصب / بررسی", "install:check")],
            [btn("🎛 پنل اصلی", "panel:main"), btn("🔒 قفل‌ها", "locks:core")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    return _INSTALL_UI_OLD_SECTION_KEYBOARD(section, cid)


def handle_panel(msg: dict, args: str) -> None:
    cid = chat_id(msg)
    if is_group(msg):
        if not require_admin(msg, "panel"):
            return
        db.ensure_group(int(cid), msg_chat(msg).get("title") or "")
        if not _is_group_installed(int(cid)):
            api.safe_send_message(cid, "اول باید ربات رو نصب کنیم 😄\nیکی از مدیرها دستور زیر رو بفرسته:\n\n<code>نصب</code>", parse_mode="HTML", reply_to_message_id=message_id(msg), reply_markup=_install_prompt_keyboard(int(cid)))
            return
    api.safe_send_message(cid, _format_main_panel(cid), reply_markup=panel_keyboard(cid))


def handle_command(msg: dict, cmd: str, args: str) -> None:
    cid = chat_id(msg)
    if cmd == "install":
        if not is_group(msg):
            api.safe_send_message(cid, "نصب برای داخل گروهه 😄\nربات رو به گروه اضافه کن، بعد داخل گروه بزن: نصب")
            return
        if not require_admin(msg, "panel"):
            return
        _run_install_for_group(msg)
        return
    if cmd in {"panel", "settings", "پنل"}:
        handle_panel(msg, args)
        return
    return _INSTALL_UI_OLD_HANDLE_COMMAND(msg, cmd, args)


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query")
    if not q:
        return False
    data = q.get("data") or ""
    msg = q.get("message") or {}
    cid = chat_id(msg)
    mid = message_id(msg)
    actor = user_id(q.get("from") or {})
    qid = q.get("id") or q.get("callback_query_id")

    if data == "install:check":
        if qid:
            api.answer_callback_query(qid)
        if not cid or not str(cid).lstrip("-").isdigit():
            return True
        if not can_manage(int(cid), actor, "panel"):
            if qid:
                api.answer_callback_query(qid, "این دکمه برای مدیرهای گروهه 😅", show_alert=True)
            return True
        db.ensure_group(int(cid), msg_chat(msg).get("title") or db.get_group_title(int(cid)) or "")
        _mark_group_installed(int(cid))
        _edit_or_send_panel(cid, mid, _install_success_text(int(cid)), panel_keyboard(int(cid)))
        return True

    if data in {"smartads:on", "smartads:off"}:
        if qid:
            api.answer_callback_query(qid)
        if not cid or not can_manage(int(cid), actor, "locks"):
            if qid:
                api.answer_callback_query(qid, "این بخش برای مدیرهای مجازه 😅", show_alert=True)
            return True
        if data == "smartads:on":
            db.update_group_setting(int(cid), "lock_ads", True)
            db.update_group_setting(int(cid), "smart_ads_enabled", True)
        else:
            db.update_group_setting(int(cid), "smart_ads_enabled", False)
        _render_section(int(cid), mid, "smartads")
        return True

    return _INSTALL_UI_OLD_HANDLE_CALLBACK(update)


def process_update(update: dict) -> None:
    try:
        if _maybe_handle_bot_added_update(update):
            return
    except Exception:
        log.exception("Install prompt detection failed")
    return _INSTALL_UI_OLD_PROCESS_UPDATE(update)



# ============================================================
# Group purge pack
# - Admin command: پاکسازی گروه / پاکسازی 100 / پاکسازی گروه کل
# - Deletes messages in safe chunks of 100, newest to oldest.
# - Stores seen message IDs from now on, but also can scan backwards by message_id.
# ============================================================

_PURGE_OLD_PROCESS_UPDATE = process_update
_PURGE_OLD_HANDLE_CALLBACK = handle_callback
_PURGE_OLD_PANEL_KEYBOARD = panel_keyboard

PURGE_CHUNK_SIZE = int(os.getenv("PURGE_CHUNK_SIZE", "100"))
PURGE_DEFAULT_SCAN = int(os.getenv("PURGE_DEFAULT_SCAN", "1000"))
PURGE_MAX_SCAN = int(os.getenv("PURGE_MAX_SCAN", "10000"))
PURGE_SLEEP_SECONDS = float(os.getenv("PURGE_SLEEP_SECONDS", "0.06"))


def ensure_purge_schema() -> None:
    with db._lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS group_messages (
            group_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(group_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_group_messages_group_mid ON group_messages(group_id, message_id DESC);
        """)


ensure_purge_schema()


def _remember_group_message(msg: dict) -> None:
    try:
        if not msg or not is_group(msg):
            return
        cid = int(chat_id(msg))
        mid = message_id(msg)
        if not mid:
            return
        uid = user_id(msg_from(msg)) or 0
        with db._lock:
            db.conn.execute(
                "INSERT OR IGNORE INTO group_messages(group_id,message_id,user_id,created_at) VALUES(?,?,?,?)",
                (cid, int(mid), int(uid), now_ts()),
            )
    except Exception:
        log.debug("Could not remember message id", exc_info=True)


def _purge_help_text() -> str:
    return (
        "🧹 پاکسازی گروه\n\n"
        "با این دستور پیام‌ها از آخر به اول پاک میشن؛ صدتا صدتا جلو میره تا فشار به ربات نیاد.\n\n"
        "نمونه‌ها:\n"
        "<code>پاکسازی 100</code>\n"
        "<code>پاکسازی گروه 1000</code>\n"
        "<code>پاکسازی گروه کل</code>\n\n"
        "نکته: ربات باید ادمین باشه و دسترسی حذف پیام داشته باشه. بعضی پیام‌های خیلی قدیمی یا پیام‌هایی که بله اجازه حذفشون رو نده، حذف نمیشن."
    )


def _parse_purge_command(text: str, current_mid: Optional[int]) -> Optional[dict]:
    t = fa_norm(text or "")
    if not t:
        return None
    starters = [
        "پاکسازی", "پاک سازی", "پاکسازی گروه", "پاک سازی گروه", "پاکسازی چت", "پاک سازی چت",
        "حذف چت", "حذف پیامها", "حذف پیام ها", "پاک کردن چت", "پاک کردن گروه",
    ]
    matched = False
    for s in sorted(starters, key=len, reverse=True):
        ns = fa_norm(s)
        if t == ns or t.startswith(ns + " "):
            matched = True
            break
    if not matched:
        return None

    if "راهنما" in t or "کمک" in t:
        return {"action": "help"}

    is_all = any(x in t.split() for x in ["کل", "همه", "کامل"])
    nums = re.findall(r"\d+", t)
    if is_all:
        requested = int(current_mid or PURGE_MAX_SCAN)
        amount = min(requested, PURGE_MAX_SCAN)
        mode = "کل"
    elif nums:
        amount = max(1, min(int(nums[0]), PURGE_MAX_SCAN))
        mode = f"{amount} پیام"
    else:
        amount = min(PURGE_DEFAULT_SCAN, PURGE_MAX_SCAN)
        mode = f"پیش‌فرض {amount} پیام"

    return {"action": "confirm", "amount": amount, "mode": mode}


def _purge_confirm_keyboard(cid: int, start_mid: int, amount: int) -> dict:
    return inline_keyboard([
        [btn("🧹 آره، پاک کن", f"purge:ok:{cid}:{start_mid}:{amount}")],
        [btn("❌ بیخیال", f"purge:no:{cid}")],
    ])


def _send_purge_confirm(msg: dict, amount: int, mode: str) -> None:
    cid = int(chat_id(msg))
    mid = int(message_id(msg) or 0)
    if not mid:
        api.safe_send_message(cid, "پیام شروع پاکسازی شناسه نداره؛ دوباره دستور رو بفرست.")
        return
    txt = (
        "⚠️ مطمئنی می‌خوای پاکسازی کنم؟\n\n"
        f"حالت: {mode}\n"
        f"حداکثر پیام برای بررسی: {amount}\n"
        f"اندازه هر مرحله: {PURGE_CHUNK_SIZE} تا\n\n"
        "بعد از تایید، پیام‌ها از همینجا به عقب پاک میشن."
    )
    api.safe_send_message(cid, txt, reply_to_message_id=mid, reply_markup=_purge_confirm_keyboard(cid, mid, amount))


def _purge_edit_status(cid: int, status_mid: Optional[int], text: str, markup: Optional[dict] = None) -> None:
    if status_mid:
        ok = api.safe_call("editMessageText", {"chat_id": cid, "message_id": status_mid, "text": text[:4096], "reply_markup": markup}, default=None)
        if ok:
            return
    api.safe_send_message(cid, text, reply_markup=markup)


def _purge_messages_backwards(cid: int, start_mid: int, amount: int, status_mid: Optional[int] = None) -> dict:
    amount = max(1, min(int(amount), PURGE_MAX_SCAN))
    start_mid = int(start_mid)
    end_mid = max(1, start_mid - amount + 1)
    deleted = 0
    failed = 0
    checked = 0
    last_progress = 0

    # آخرین پیام‌های ذخیره‌شده را هم نگه می‌داریم، اما روش اصلی اسکن شناسه‌های پشت سر هم است.
    ids = list(range(start_mid, end_mid - 1, -1))

    for i in range(0, len(ids), PURGE_CHUNK_SIZE):
        chunk = ids[i:i + PURGE_CHUNK_SIZE]
        for mid in chunk:
            checked += 1
            if api.safe_delete_message(cid, mid):
                deleted += 1
                with db._lock:
                    db.conn.execute("DELETE FROM group_messages WHERE group_id=? AND message_id=?", (cid, int(mid)))
            else:
                failed += 1
            if PURGE_SLEEP_SECONDS > 0:
                time.sleep(PURGE_SLEEP_SECONDS)
        if status_mid and checked - last_progress >= PURGE_CHUNK_SIZE:
            last_progress = checked
            _purge_edit_status(
                cid,
                status_mid,
                f"🧹 دارم پاکسازی می‌کنم...\n\nبررسی‌شده: {checked}/{amount}\nحذف‌شده: {deleted}\nردشده/ناموفق: {failed}\n\nیه کم صبر کن، صدتا صدتا جلو میرم.",
            )

    db.log(cid, None, "purge_group", {"start_mid": start_mid, "checked": checked, "deleted": deleted, "failed": failed})
    return {"checked": checked, "deleted": deleted, "failed": failed, "start_mid": start_mid, "end_mid": end_mid}


def handle_purge_command(msg: dict, parsed: dict) -> bool:
    cid = chat_id(msg)
    if not is_group(msg):
        api.safe_send_message(cid, "پاکسازی برای داخل گروهه 😄")
        return True
    if not require_admin(msg, "all"):
        return True
    if parsed.get("action") == "help":
        api.safe_send_message(cid, _purge_help_text(), reply_to_message_id=message_id(msg), parse_mode="HTML")
        return True
    _send_purge_confirm(msg, int(parsed["amount"]), parsed.get("mode", "پاکسازی"))
    return True


def _handle_purge_callback(update: dict) -> bool:
    q = update.get("callback_query") or {}
    data = q.get("data") or ""
    if not data.startswith("purge:"):
        return False
    qid = q.get("id") or q.get("callback_query_id")
    msg = q.get("message") or {}
    cid = chat_id(msg)
    mid = message_id(msg)
    actor = user_id(q.get("from") or {})
    parts = data.split(":")

    if qid:
        api.answer_callback_query(qid, "")

    if len(parts) >= 3 and parts[1] == "no":
        _purge_edit_status(int(cid), mid, "اوکی، پاکسازی لغو شد ✅")
        return True

    if len(parts) != 5 or parts[1] != "ok":
        return True

    target_cid = int(parts[2])
    start_mid = int(parts[3])
    amount = int(parts[4])

    if not cid or int(cid) != target_cid:
        return True
    if not can_manage(target_cid, actor, "all"):
        if qid:
            api.answer_callback_query(qid, "این دکمه فقط برای مدیرهای گروهه 😅", show_alert=True)
        return True

    _purge_edit_status(target_cid, mid, "🧹 شروع کردم...\nپیام‌ها رو صدتا صدتا پاک می‌کنم.")
    result = _purge_messages_backwards(target_cid, start_mid, amount, mid)
    final = (
        "✅ پاکسازی تموم شد\n\n"
        f"بررسی‌شده: {result['checked']} پیام\n"
        f"حذف‌شده: {result['deleted']} پیام\n"
        f"ناموفق/غیرقابل‌حذف: {result['failed']} پیام\n\n"
        "اگه می‌خوای عقب‌تر هم پاک بشه، دوباره بزن:\n"
        "<code>پاکسازی گروه کل</code>"
    )
    _purge_edit_status(target_cid, mid, final)
    return True



# ---------------------------------------------------------------------------
# Purge button in pretty panel
# ---------------------------------------------------------------------------

_PURGE_PANEL_OLD_PANEL_KEYBOARD = panel_keyboard
_PURGE_PANEL_OLD_PANEL_TEXT = _panel_text
_PURGE_PANEL_OLD_SECTION_KEYBOARD = _section_keyboard
_PURGE_PANEL_OLD_HANDLE_PURGE_CALLBACK = _handle_purge_callback


def _purge_panel_text(cid: int) -> str:
    return (
        "🧹 پاکسازی گروه\n"
        "━━━━━━━━━━━━━━\n\n"
        "از اینجا می‌تونی چت‌های گروه رو تمیز کنی.\n"
        "پاکسازی از آخرین پیام به عقب انجام میشه و ربات پیام‌ها رو صدتا صدتا پاک می‌کنه تا فشار نخوره.\n\n"
        "⚠️ حواست باشه: بعد از تایید، پیام‌ها واقعاً حذف میشن.\n"
        "ربات هم باید ادمین باشه و دسترسی حذف پیام داشته باشه.\n\n"
        f"حداکثر بررسی فعلی: {PURGE_MAX_SCAN} پیام\n"
        f"اندازه هر مرحله: {PURGE_CHUNK_SIZE} پیام"
    )


def _panel_text(section: str, cid: int) -> str:
    if section == "purge":
        return _purge_panel_text(cid)
    return _PURGE_PANEL_OLD_PANEL_TEXT(section, cid)


def _section_keyboard(section: str, cid: int) -> dict:
    if section == "purge":
        return inline_keyboard([
            [btn("🧹 پاکسازی ۱۰۰ پیام", "purge:start:100")],
            [btn("🧹 پاکسازی ۵۰۰ پیام", "purge:start:500"), btn("🧹 پاکسازی ۱۰۰۰ پیام", "purge:start:1000")],
            [btn("🧨 پاکسازی کل چت", "purge:start:all")],
            [btn("📋 کپی دستور ۱۰۰", copy_text="پاکسازی 100"), btn("📋 کپی دستور کل", copy_text="پاکسازی گروه کل")],
            [btn("⬅️ برگشت", "panel:main")],
        ])
    return _PURGE_PANEL_OLD_SECTION_KEYBOARD(section, cid)


def panel_keyboard(group_id: Optional[int] = None) -> dict:
    base = _PURGE_PANEL_OLD_PANEL_KEYBOARD(group_id)
    try:
        rows = list(base.get("inline_keyboard") or [])
        # اگر قبلاً دکمه کپی پاکسازی با پچ قبلی اضافه شده بود حذفش می‌کنیم تا پنل شلوغ نشه.
        cleaned = []
        for row in rows:
            labels = [str((b or {}).get("text", "")) for b in row]
            if any("پاکسازی" in x for x in labels) and any("copy_text" in (b or {}) for b in row):
                continue
            cleaned.append(row)
        rows = cleaned
        # جایگاه واضح: کنار بکاپ و ابزارهای سریع
        inserted = False
        for i, row in enumerate(rows):
            labels = " ".join(str((b or {}).get("text", "")) for b in row)
            if "بکاپ" in labels or "راهنمای سریع" in labels:
                rows.insert(i, [btn("🧹 پاکسازی گروه", "p:purge"), btn("💾 بکاپ", "p:backup")])
                inserted = True
                # اگر ردیف اصلی فقط بکاپ/راهنما بود، بکاپ تکراری را کمی مرتب می‌کنیم.
                if "بکاپ" in labels:
                    new_row = [b for b in row if "بکاپ" not in str((b or {}).get("text", ""))]
                    if new_row:
                        rows[i + 1] = new_row
                    else:
                        rows.pop(i + 1)
                break
        if not inserted:
            rows.append([btn("🧹 پاکسازی گروه", "p:purge")])
        return inline_keyboard(rows)
    except Exception:
        return base


def _handle_purge_callback(update: dict) -> bool:
    q = update.get("callback_query") or {}
    data = q.get("data") or ""
    if not data.startswith("purge:"):
        return False

    qid = q.get("id") or q.get("callback_query_id")
    msg = q.get("message") or {}
    cid = chat_id(msg)
    mid = int(message_id(msg) or 0)
    actor = user_id(q.get("from") or {})

    if qid:
        api.answer_callback_query(qid, "")

    if not cid or not mid:
        return True
    target_cid = int(cid)

    # شروع از پنل: اول تایید می‌گیریم. start_mid را یک عدد عقب‌تر می‌گذاریم که خود پیام پنل/وضعیت پاک نشود.
    if data.startswith("purge:start:"):
        if not can_manage(target_cid, actor, "all"):
            if qid:
                api.answer_callback_query(qid, "پاکسازی فقط برای مدیرهای اصلیه 😅", show_alert=True)
            return True
        raw = data.split(":", 2)[2]
        if raw == "all":
            amount = min(max(1, mid - 1), PURGE_MAX_SCAN)
            mode = "کل چت تا جای ممکن"
        else:
            amount = max(1, min(int(raw), PURGE_MAX_SCAN))
            mode = f"{amount} پیام"
        start_mid = max(1, mid - 1)
        txt = (
            "⚠️ مطمئنی پاکسازی کنم؟\n\n"
            f"حالت: {mode}\n"
            f"حداکثر پیام برای بررسی: {amount}\n"
            f"هر مرحله: {PURGE_CHUNK_SIZE} پیام\n\n"
            "بعد از تایید، پیام‌ها از همینجا به عقب پاک میشن."
        )
        _purge_edit_status(target_cid, mid, txt, _purge_confirm_keyboard(target_cid, start_mid, amount))
        return True

    return _PURGE_PANEL_OLD_HANDLE_PURGE_CALLBACK(update)


def process_update(update: dict) -> None:
    try:
        if _handle_purge_callback(update):
            return
        msg = get_message(update)
        if msg and is_group(msg):
            _remember_group_message(msg)
            parsed = _parse_purge_command(get_text(msg), message_id(msg))
            if parsed:
                handle_purge_command(msg, parsed)
                return
        return _PURGE_OLD_PROCESS_UPDATE(update)
    except Exception:
        log.exception("Purge update processing failed: %s", json.dumps(update, ensure_ascii=False)[:1500])



if __name__ == "__main__":
    polling_loop()

# ---------------------------------------------------------------------------
# Purge fast-finish patch
# ---------------------------------------------------------------------------
# هدف: وقتی پیام قابل‌حذف دیگه پیدا نمی‌شود، پاکسازی تا عدد ۵۰۰/۱۰۰۰ بی‌دلیل معطل نشود.
# با چند خطای پشت سر هم متوجه می‌شویم به انتهای پیام‌های قابل حذف رسیده‌ایم و سریع نتیجه می‌دهیم.

PURGE_STOP_AFTER_CONSECUTIVE_FAILS = int(os.getenv("PURGE_STOP_AFTER_CONSECUTIVE_FAILS", "35"))
PURGE_PROGRESS_EVERY = int(os.getenv("PURGE_PROGRESS_EVERY", "100"))
PURGE_SLEEP_ON_SUCCESS_SECONDS = float(os.getenv("PURGE_SLEEP_ON_SUCCESS_SECONDS", str(PURGE_SLEEP_SECONDS)))


def _purge_messages_backwards(cid: int, start_mid: int, amount: int, status_mid: Optional[int] = None) -> dict:
    amount = max(1, min(int(amount), PURGE_MAX_SCAN))
    start_mid = max(1, int(start_mid))
    end_mid = max(1, start_mid - amount + 1)

    deleted = 0
    failed = 0
    checked = 0
    last_progress = 0
    consecutive_failed = 0
    stopped_early = False
    stop_reason = ""

    ids = range(start_mid, end_mid - 1, -1)

    for mid in ids:
        checked += 1
        ok = api.safe_delete_message(cid, mid)
        if ok:
            deleted += 1
            consecutive_failed = 0
            with db._lock:
                db.conn.execute("DELETE FROM group_messages WHERE group_id=? AND message_id=?", (cid, int(mid)))
            if PURGE_SLEEP_ON_SUCCESS_SECONDS > 0:
                time.sleep(PURGE_SLEEP_ON_SUCCESS_SECONDS)
        else:
            failed += 1
            consecutive_failed += 1
            # روی پیام‌های ناموجود/غیرقابل‌حذف دیلی نمی‌زنیم تا پاکسازی بی‌دلیل طول نکشد.

        # اگر تعداد زیادی پیام پشت‌سرهم قابل حذف نبود، احتمالاً رسیدیم به جایی که چیزی برای حذف نیست.
        if consecutive_failed >= max(5, PURGE_STOP_AFTER_CONSECUTIVE_FAILS):
            stopped_early = True
            stop_reason = f"{consecutive_failed} پیام پشت‌سرهم پیدا/حذف نشد"
            break

        if status_mid and checked - last_progress >= max(10, PURGE_PROGRESS_EVERY):
            last_progress = checked
            _purge_edit_status(
                cid,
                status_mid,
                f"🧹 دارم پاکسازی می‌کنم...\n\nبررسی‌شده: {checked}\nحذف‌شده: {deleted}\nناموفق/غیرقابل‌حذف: {failed}\n\nاگه پیام قابل‌حذف دیگه پیدا نشه، خودم سریع تمومش می‌کنم.",
            )

    db.log(cid, None, "purge_group", {
        "start_mid": start_mid,
        "checked": checked,
        "deleted": deleted,
        "failed": failed,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
    })
    return {
        "checked": checked,
        "deleted": deleted,
        "failed": failed,
        "start_mid": start_mid,
        "end_mid": end_mid,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
    }


def _purge_final_text(result: dict) -> str:
    if int(result.get("deleted") or 0) > 0:
        title = "✅ پاک شد"
        note = "چت تا جایی که پیام قابل‌حذف پیدا شد، تمیز شد 😎"
    else:
        title = "✅ چیزی برای پاک کردن پیدا نکردم"
        note = "یا پیام قابل‌حذفی نبود، یا بله اجازه حذف اون پیام‌ها رو نداد."

    extra = ""
    if result.get("stopped_early"):
        extra = f"\n\n⏱ برای اینکه الکی معطل نشه، بعد از چند پیام ناموفق پشت‌سرهم متوقف شد."

    return (
        f"{title}\n\n"
        f"حذف‌شده: {result.get('deleted', 0)} پیام\n"
        f"بررسی‌شده: {result.get('checked', 0)} پیام\n"
        f"ناموفق/غیرقابل‌حذف: {result.get('failed', 0)} پیام"
        f"{extra}\n\n"
        f"{note}"
    )


def _handle_purge_callback(update: dict) -> bool:
    q = update.get("callback_query") or {}
    data = q.get("data") or ""
    if not data.startswith("purge:"):
        return False

    qid = q.get("id") or q.get("callback_query_id")
    msg = q.get("message") or {}
    cid = chat_id(msg)
    mid = int(message_id(msg) or 0)
    actor = user_id(q.get("from") or {})

    if qid:
        api.answer_callback_query(qid, "")

    if not cid or not mid:
        return True
    target_cid = int(cid)

    if data.startswith("purge:start:"):
        if not can_manage(target_cid, actor, "all"):
            if qid:
                api.answer_callback_query(qid, "پاکسازی فقط برای مدیرهای اصلیه 😅", show_alert=True)
            return True
        raw = data.split(":", 2)[2]
        start_mid = max(1, mid - 1)
        if raw == "all":
            amount = min(start_mid, PURGE_MAX_SCAN)
            mode = "کل چت تا جای ممکن"
        else:
            amount = max(1, min(int(raw), PURGE_MAX_SCAN, start_mid))
            mode = f"{amount} پیام"
        txt = (
            "⚠️ مطمئنی پاکسازی کنم؟\n\n"
            f"حالت: {mode}\n"
            f"حداکثر بررسی: {amount} پیام\n"
            "اگه پیام قابل‌حذف زودتر تموم بشه، معطل نمی‌کنم و سریع میگم پاک شد ✅\n\n"
            "بعد از تایید، پیام‌ها از همینجا به عقب پاک میشن."
        )
        _purge_edit_status(target_cid, mid, txt, _purge_confirm_keyboard(target_cid, start_mid, amount))
        return True

    parts = data.split(":")

    if len(parts) >= 3 and parts[1] == "no":
        _purge_edit_status(target_cid, mid, "اوکی، پاکسازی لغو شد ✅")
        return True

    if len(parts) == 5 and parts[1] == "ok":
        confirm_cid = int(parts[2])
        start_mid = int(parts[3])
        amount = int(parts[4])
        if confirm_cid != target_cid:
            return True
        if not can_manage(target_cid, actor, "all"):
            if qid:
                api.answer_callback_query(qid, "این دکمه فقط برای مدیرهای گروهه 😅", show_alert=True)
            return True
        amount = max(1, min(amount, PURGE_MAX_SCAN, max(1, start_mid)))
        _purge_edit_status(target_cid, mid, "🧹 شروع کردم...\nاگه پیام‌ها زودتر تموم بشن، سریع اعلام می‌کنم پاک شد ✅")
        result = _purge_messages_backwards(target_cid, start_mid, amount, mid)
        _purge_edit_status(target_cid, mid, _purge_final_text(result))
        return True

    return True

