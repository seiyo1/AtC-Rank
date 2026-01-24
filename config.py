import os

from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
SQLITE_PATH = os.getenv("SQLITE_PATH", "atcrank.db")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))
INITIAL_FETCH_EPOCH = int(os.getenv("INITIAL_FETCH_EPOCH", "1768748400"))

AI_ENABLED = _get_bool("AI_ENABLED", True)
AI_PROBABILITY = int(os.getenv("AI_PROBABILITY", "20"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
_raw_ai_model = os.getenv("AI_MODEL", "gpt-5-mini")
_notify_models_env = os.getenv("AI_MODELS_NOTIFY", "")
_notify_models = _parse_csv(_notify_models_env) if _notify_models_env else _parse_csv(_raw_ai_model)
AI_MODEL = _notify_models[0] if _notify_models else _raw_ai_model
AI_MODELS_NOTIFY = _notify_models or [AI_MODEL]
_raw_celebration = os.getenv("AI_MODEL_CELEBRATION", AI_MODEL)
_celebration_list = _parse_csv(_raw_celebration)
AI_MODEL_CELEBRATION = _celebration_list[0] if _celebration_list else _raw_celebration

PROBLEMS_SYNC_INTERVAL_SECONDS = int(os.getenv("PROBLEMS_SYNC_INTERVAL_SECONDS", "21600"))
HEALTHCHECK_INTERVAL_SECONDS = int(os.getenv("HEALTHCHECK_INTERVAL_SECONDS", "21600"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/atcrank.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "1048576"))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
