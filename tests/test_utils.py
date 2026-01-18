import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from utils import display_difficulty, week_start_jst


def test_display_difficulty_above_400():
    assert display_difficulty(400) == 400


def test_display_difficulty_below_400():
    raw = 0
    expected = round(400.0 / math.exp(1.0 - raw / 400.0))
    assert display_difficulty(raw) == expected


def test_week_start_jst():
    # 2026-01-18 00:00 UTC is 2026-01-18 09:00 JST (Sunday)
    dt = datetime(2026, 1, 18, 0, 0, tzinfo=timezone.utc)
    week_start = week_start_jst(dt)
    jst = ZoneInfo("Asia/Tokyo")
    week_start_jst_dt = week_start.astimezone(jst)
    assert week_start_jst_dt.year == 2026
    assert week_start_jst_dt.month == 1
    assert week_start_jst_dt.day == 12
    assert week_start_jst_dt.hour == 7
    assert week_start_jst_dt.minute == 0
