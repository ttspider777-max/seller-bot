import os
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN", "")
ADMIN_ID = 8717087613
WIDGET_MENUS = os.getenv("WIDGET_MENUS", "1") != "0"
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "data.db")
