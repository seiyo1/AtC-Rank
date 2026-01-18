import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_jst(dt: datetime) -> datetime:
    return dt.astimezone(JST)


def week_start_jst(dt: datetime) -> datetime:
    jst = to_jst(dt)
    monday = (jst - timedelta(days=jst.weekday())).replace(
        hour=7, minute=0, second=0, microsecond=0
    )
    if jst < monday:
        monday -= timedelta(days=7)
    return monday.astimezone(timezone.utc)


def next_week_start_jst(dt: datetime) -> datetime:
    current = week_start_jst(dt)
    return current + timedelta(days=7)


def display_difficulty(difficulty_raw: float) -> int:
    if difficulty_raw < 400:
        return round(400.0 / math.exp(1.0 - difficulty_raw / 400.0))
    return round(difficulty_raw)


def color_key(value: int | None) -> str:
    if value is None or value <= 0:
        return "gray"
    if value < 400:
        return "gray"
    if value < 800:
        return "brown"
    if value < 1200:
        return "green"
    if value < 1600:
        return "cyan"
    if value < 2000:
        return "blue"
    if value < 2400:
        return "yellow"
    if value < 2800:
        return "orange"
    return "red"


COLOR_EMOJI = {
    "gray": "⬜",
    "brown": "🟫",
    "green": "🟩",
    "cyan": "💧",
    "blue": "🫐",
    "yellow": "🟨",
    "orange": "🟧",
    "red": "🟥",
}

COLOR_NAMES = {
    "gray": "Gray",
    "brown": "Brown",
    "green": "Green",
    "cyan": "Cyan",
    "blue": "Blue",
    "yellow": "Yellow",
    "orange": "Orange",
    "red": "Red",
}


ROLE_LABELS = {
    "gray": "⬜ Gray",
    "brown": "🟫 Brown",
    "green": "🟩 Green",
    "cyan": "💧 Cyan",
    "blue": "🫐 Blue",
    "yellow": "🟨 Yellow",
    "orange": "🟧 Orange",
    "red": "🟥 Red",
}
