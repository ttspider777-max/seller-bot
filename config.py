import os
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN", "")
ADMIN_ID = 8717087613  # единственный владелец/админ
DB_PATH = str(BASE / "data.db")
