import os

from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
SQLITE_PATH = os.getenv("SQLITE_PATH", "atcrank.db")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))

AI_ENABLED = _get_bool("AI_ENABLED", True)
AI_PROBABILITY = int(os.getenv("AI_PROBABILITY", "20"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-5-nano")

PROBLEMS_SYNC_INTERVAL_SECONDS = int(os.getenv("PROBLEMS_SYNC_INTERVAL_SECONDS", "21600"))
HEALTHCHECK_INTERVAL_SECONDS = int(os.getenv("HEALTHCHECK_INTERVAL_SECONDS", "21600"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/atcrank.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "1048576"))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
