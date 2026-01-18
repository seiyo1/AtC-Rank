from __future__ import annotations

import pathlib
from datetime import date, datetime
from typing import Any, Iterable

import aiosqlite

from config import INITIAL_FETCH_EPOCH
SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")


def _dt_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _date_to_str(value: date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _str_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _str_to_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


async def create_db(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn


async def init_db(conn: aiosqlite.Connection) -> None:
    schema = SCHEMA_PATH.read_text()
    await conn.executescript(schema)
    await conn.execute(
        "update user_fetch_state set last_checked_epoch=? where last_checked_epoch=0",
        (INITIAL_FETCH_EPOCH,),
    )
    await conn.commit()


async def ensure_settings(conn: aiosqlite.Connection, guild_id: int) -> None:
    await conn.execute(
        "insert into settings (guild_id) values (?) on conflict do nothing",
        (guild_id,),
    )
    await conn.commit()


async def get_settings(conn: aiosqlite.Connection, guild_id: int) -> dict[str, Any]:
    cursor = await conn.execute("select * from settings where guild_id=?", (guild_id,))
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def update_setting(conn: aiosqlite.Connection, guild_id: int, field: str, value: Any) -> None:
    await conn.execute(
        f"update settings set {field}=? where guild_id=?",
        (value, guild_id),
    )
    await conn.commit()


async def upsert_user(conn: aiosqlite.Connection, discord_id: int, atcoder_id: str) -> None:
    atcoder_id = atcoder_id.strip()
    await conn.execute(
        """
        insert into users (discord_id, atcoder_id)
        values (?, ?)
        on conflict (discord_id) do update set atcoder_id=excluded.atcoder_id, is_active=1
        """,
        (discord_id, atcoder_id),
    )
    await conn.execute(
        "insert into user_fetch_state (discord_id, last_checked_epoch) values (?, ?) on conflict do nothing",
        (discord_id, INITIAL_FETCH_EPOCH),
    )
    await conn.execute(
        "insert into streaks (discord_id) values (?) on conflict do nothing",
        (discord_id,),
    )
    await conn.commit()


async def deactivate_user(conn: aiosqlite.Connection, discord_id: int) -> None:
    await conn.execute("update users set is_active=0 where discord_id=?", (discord_id,))
    await conn.commit()


async def get_active_users(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute("select discord_id, atcoder_id from users where is_active=1")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_user_atcoder_id(conn: aiosqlite.Connection, discord_id: int) -> str | None:
    cursor = await conn.execute("select atcoder_id from users where discord_id=?", (discord_id,))
    row = await cursor.fetchone()
    return row["atcoder_id"] if row else None


async def get_fetch_state(conn: aiosqlite.Connection, discord_id: int) -> dict[str, Any]:
    cursor = await conn.execute("select * from user_fetch_state where discord_id=?", (discord_id,))
    row = await cursor.fetchone()
    return dict(row) if row else {"last_checked_epoch": INITIAL_FETCH_EPOCH, "last_submission_id": None}


async def update_fetch_state(conn: aiosqlite.Connection, discord_id: int, last_epoch: int, last_submission_id: int | None) -> None:
    await conn.execute(
        """
        insert into user_fetch_state (discord_id, last_checked_epoch, last_submission_id)
        values (?, ?, ?)
        on conflict (discord_id) do update set last_checked_epoch=excluded.last_checked_epoch,
                                              last_submission_id=excluded.last_submission_id
        """,
        (discord_id, last_epoch, last_submission_id),
    )
    await conn.commit()


async def upsert_problems(conn: aiosqlite.Connection, problems: Iterable[dict[str, Any]]) -> None:
    await conn.executemany(
        """
        insert into problems (problem_id, contest_id, title, difficulty_raw, difficulty)
        values (?, ?, ?, ?, ?)
        on conflict (problem_id) do update set
          contest_id=excluded.contest_id,
          title=excluded.title,
          difficulty_raw=excluded.difficulty_raw,
          difficulty=excluded.difficulty
        """,
        [
            (
                p["problem_id"],
                p.get("contest_id"),
                p.get("title"),
                p.get("difficulty_raw"),
                p.get("difficulty"),
            )
            for p in problems
        ],
    )
    await conn.commit()


async def get_problem(conn: aiosqlite.Connection, problem_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute("select * from problems where problem_id=?", (problem_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_rating(conn: aiosqlite.Connection, discord_id: int, rating: int) -> None:
    await conn.execute(
        """
        insert into ratings (discord_id, rating, updated_at)
        values (?, ?, CURRENT_TIMESTAMP)
        on conflict (discord_id) do update set rating=excluded.rating, updated_at=CURRENT_TIMESTAMP
        """,
        (discord_id, rating),
    )
    await conn.commit()


async def get_rating(conn: aiosqlite.Connection, discord_id: int) -> int:
    cursor = await conn.execute("select rating from ratings where discord_id=?", (discord_id,))
    row = await cursor.fetchone()
    return int(row["rating"]) if row and row["rating"] is not None else 0


async def get_last_ac(conn: aiosqlite.Connection, discord_id: int, problem_id: str) -> datetime | None:
    cursor = await conn.execute(
        "select last_ac_at from user_problem_last_ac where discord_id=? and problem_id=?",
        (discord_id, problem_id),
    )
    row = await cursor.fetchone()
    return _str_to_dt(row["last_ac_at"]) if row else None


async def upsert_last_ac(conn: aiosqlite.Connection, discord_id: int, problem_id: str, last_ac_at: datetime) -> None:
    await conn.execute(
        """
        insert into user_problem_last_ac (discord_id, problem_id, last_ac_at)
        values (?, ?, ?)
        on conflict (discord_id, problem_id) do update set last_ac_at=excluded.last_ac_at
        """,
        (discord_id, problem_id, _dt_to_str(last_ac_at)),
    )
    await conn.commit()


async def get_streak(conn: aiosqlite.Connection, discord_id: int) -> dict[str, Any]:
    cursor = await conn.execute(
        "select current_streak, last_ac_date from streaks where discord_id=?",
        (discord_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return {"current_streak": 0, "last_ac_date": None}
    return {
        "current_streak": row["current_streak"],
        "last_ac_date": _str_to_date(row["last_ac_date"]),
    }


async def update_streak(conn: aiosqlite.Connection, discord_id: int, current_streak: int, last_ac_date: date) -> None:
    await conn.execute(
        """
        insert into streaks (discord_id, current_streak, last_ac_date)
        values (?, ?, ?)
        on conflict (discord_id) do update set current_streak=excluded.current_streak,
                                              last_ac_date=excluded.last_ac_date
        """,
        (discord_id, current_streak, _date_to_str(last_ac_date)),
    )
    await conn.commit()


async def add_weekly_score(
    conn: aiosqlite.Connection,
    week_start: datetime,
    discord_id: int,
    score_delta: int,
) -> None:
    await conn.execute(
        """
        insert into weekly_scores (week_start, discord_id, score, score_updated_at)
        values (?, ?, ?, CURRENT_TIMESTAMP)
        on conflict (week_start, discord_id) do update
          set score = weekly_scores.score + excluded.score,
              score_updated_at = CURRENT_TIMESTAMP
        """,
        (_dt_to_str(week_start), discord_id, score_delta),
    )
    await conn.commit()


async def get_weekly_scores(conn: aiosqlite.Connection, week_start: datetime) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        select w.discord_id, w.score, w.score_updated_at, u.atcoder_id
        from weekly_scores w
        left join users u on w.discord_id = u.discord_id
        where w.week_start=?
        order by w.score desc, w.score_updated_at asc
        """,
        (_dt_to_str(week_start),),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_weekly_score(conn: aiosqlite.Connection, week_start: datetime, discord_id: int) -> int:
    cursor = await conn.execute(
        "select score from weekly_scores where week_start=? and discord_id=?",
        (_dt_to_str(week_start), discord_id),
    )
    row = await cursor.fetchone()
    return int(row["score"]) if row else 0


async def upsert_weekly_report(
    conn: aiosqlite.Connection,
    week_start: datetime,
    reset_time: datetime,
    report_text: str,
    ai_comment: str | None,
) -> None:
    await conn.execute(
        """
        insert into weekly_reports (week_start, reset_time, report_text, ai_comment)
        values (?, ?, ?, ?)
        on conflict (week_start) do update
          set reset_time=excluded.reset_time,
              report_text=excluded.report_text,
              ai_comment=excluded.ai_comment
        """,
        (_dt_to_str(week_start), _dt_to_str(reset_time), report_text, ai_comment),
    )
    await conn.commit()


async def insert_submission(
    conn: aiosqlite.Connection,
    discord_id: int,
    problem_id: str,
    submitted_at: datetime,
    score_base: int,
    streak_mult: float,
    score_final: int,
) -> None:
    await conn.execute(
        """
        insert into submissions (discord_id, problem_id, submitted_at, score_base, streak_mult, score_final)
        values (?, ?, ?, ?, ?, ?)
        """,
        (discord_id, problem_id, _dt_to_str(submitted_at), score_base, streak_mult, score_final),
    )
    await conn.commit()


async def store_role_color(conn: aiosqlite.Connection, guild_id: int, color_key: str, role_id: int) -> None:
    await conn.execute(
        """
        insert into role_colors (guild_id, color_key, role_id)
        values (?, ?, ?)
        on conflict (guild_id, color_key) do update set role_id=excluded.role_id
        """,
        (guild_id, color_key, role_id),
    )
    await conn.commit()


async def get_role_colors(conn: aiosqlite.Connection, guild_id: int) -> dict[str, int]:
    cursor = await conn.execute("select color_key, role_id from role_colors where guild_id=?", (guild_id,))
    rows = await cursor.fetchall()
    return {r["color_key"]: r["role_id"] for r in rows}
