import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("UIL_DATA_DIR", BASE_DIR / "data"))
PACKETS_DIR = Path(os.getenv("UIL_PACKETS_DIR", DATA_DIR / "packets"))
DB_PATH = Path(os.getenv("UIL_DB_PATH", DATA_DIR / "bot.sqlite3"))

TOKEN = os.getenv("DISCORD_TOKEN", "")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "1") not in ("0", "false", "False")

WRONG_ANSWER_COOLDOWN = float(os.getenv("WRONG_ANSWER_COOLDOWN", "10"))
QUIZ_WINDOW = int(os.getenv("QUIZ_WINDOW_MINUTES", "10")) * 60
MIN_INTERVAL = int(os.getenv("MIN_INTERVAL_MINUTES", "5")) * 60
MAX_INTERVAL = 30 * 24 * 3600
MAX_WINDOW = 7 * 24 * 3600
REINDEX_MINUTES = int(os.getenv("REINDEX_MINUTES", "10"))
