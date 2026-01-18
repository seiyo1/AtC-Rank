import asyncio
import os
import tempfile
from datetime import datetime, timezone

import pytest

import db


@pytest.mark.asyncio
async def test_db_basic_flow():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = await db.create_db(path)
        await db.init_db(conn)
        await db.ensure_settings(conn, 123)
        await db.upsert_user(conn, 1, "alice")
        users = await db.get_active_users(conn)
        assert users[0]["atcoder_id"] == "alice"

        week_start = datetime(2026, 1, 12, 7, 0, tzinfo=timezone.utc)
        await db.add_weekly_score(conn, week_start, 1, 100)
        await db.add_weekly_score(conn, week_start, 1, 50)
        score = await db.get_weekly_score(conn, week_start, 1)
        assert score == 150

        await db.update_fetch_state(conn, 1, 100, 5)
        state = await db.get_fetch_state(conn, 1)
        assert state["last_checked_epoch"] == 100
        assert state["last_submission_id"] == 5

        await conn.close()
    finally:
        if os.path.exists(path):
            os.remove(path)
