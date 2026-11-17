import os
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN", "")
# Optional marketplace session credentials. Keep these only in the local .env.
PLAYEROK_COOKIE = os.getenv("PLAYEROK_COOKIE", "")
# Compatibility with installations that already use the token-only setting.
PLAYEROK_TOKEN = os.getenv("PLAYEROK_TOKEN", "")
STARVELL_SESSION = os.getenv("STARVELL_SESSION", "")
ADMIN_ID = 8717087613  # единственный владелец/админ
DB_PATH = str(BASE / "data.db")
