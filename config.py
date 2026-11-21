import os
import re


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

_raw_admins = os.environ.get("ADMIN_IDS", "") or os.environ.get("ADMIN_ID", "")
ADMIN_IDS = [int(x) for x in re.split(r"[,\s]+", _raw_admins) if x.strip().lstrip("-").isdigit()]

BRAND = os.environ.get("BOT_NAME", "ZAD VOTE BOT").strip() or "ZAD VOTE BOT"
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "zadosint").strip().lstrip("@")
WELCOME_IMAGE = os.environ.get("WELCOME_IMAGE", "").strip()

TZ_OFFSET_MIN = _int("TZ_OFFSET_MIN", 330)  # 330 = India (IST)
TZ_NAME = os.environ.get("TZ_NAME", "IST")

PORT = _int("PORT", 10000)
DB_PATH = os.environ.get("DB_PATH", "data/votebot.db")
BACKUP_HOURS = _int("BACKUP_HOURS", 6)
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL", "") or os.environ.get("RENDER_EXTERNAL_URL", "")
WORKERS = _int("WORKERS", 16)
