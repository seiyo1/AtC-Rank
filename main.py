from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import atcoder_api
import db
from ai import generate_message
from config import (
    AI_ENABLED,
    AI_PROBABILITY,
    DISCORD_TOKEN,
    GUILD_ID,
    POLL_INTERVAL_SECONDS,
    PROBLEMS_SYNC_INTERVAL_SECONDS,
    HEALTHCHECK_INTERVAL_SECONDS,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    SQLITE_PATH,
)
from scoring import base_score, streak_multiplier
from templates import NOTIFY_TEMPLATES
from utils import (
    COLOR_EMOJI,
    ROLE_LABELS,
    color_key,
    display_difficulty,
    next_week_start_jst,
    now_utc,
    to_jst,
    week_start_jst,
)


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    log_dir = os.path.dirname(LOG_FILE) or "."
    os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    handlers = []
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers.append(console)
    try:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except Exception:
        # fallback to console only
        pass

    logging.basicConfig(level=level, handlers=handlers)


setup_logging()
logger = logging.getLogger("atcrank")

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

pool = None
session: aiohttp.ClientSession | None = None
started_at = now_utc()
last_poll_at: datetime | None = None
last_problems_sync_at: datetime | None = None
last_ratings_sync_at: datetime | None = None

COLOR_VALUES = {
    "gray": 0x808080,
    "brown": 0x804000,
    "green": 0x008000,
    "cyan": 0x00C0C0,
    "blue": 0x0000FF,
    "yellow": 0xC0C000,
    "orange": 0xFF8000,
    "red": 0xFF0000,
}


@bot.event
async def on_ready() -> None:
    global pool, session
    if not SQLITE_PATH:
        raise RuntimeError("SQLITE_PATH is required")
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is required")

    try:
        pool = await db.create_db(SQLITE_PATH)
        await db.init_db(pool)
    except Exception:
        logger.exception("DB init failed")
        raise

    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild:
        await db.ensure_settings(pool, guild.id)
    session = aiohttp.ClientSession()

    await sync_problems()
    if guild:
        await ensure_color_roles(guild)

    if GUILD_ID:
        bot.tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    else:
        await bot.tree.sync()

    bot.loop.create_task(polling_loop())
    bot.loop.create_task(weekly_loop())
    bot.loop.create_task(problems_sync_loop())
    bot.loop.create_task(healthcheck_loop())
    logger.info("Bot ready")


@bot.event
async def on_close() -> None:
    if session:
        await session.close()
    if pool:
        await pool.close()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    logger.exception("app command error: %s", error)
    if interaction.response.is_done():
        return
    await interaction.response.send_message("コマンドでエラーが発生しました", ephemeral=True)


async def sync_problems() -> None:
    if not session or not pool:
        return
    try:
        models = await atcoder_api.fetch_problem_models(session)
    except Exception:
        logger.exception("failed to fetch problem models")
        return
    model_map = {m["problem_id"]: m.get("difficulty") for m in models}
    try:
        problems = await atcoder_api.fetch_problems(session)
    except Exception:
        logger.exception("failed to fetch problems")
        return
    payload = []
    for p in problems:
        problem_id = p.get("id") or p.get("problem_id")
        if not problem_id:
            continue
        raw = model_map.get(problem_id)
        difficulty = display_difficulty(raw) if raw is not None else None
        payload.append(
            {
                "problem_id": problem_id,
                "contest_id": p.get("contest_id"),
                "title": p.get("title") or p.get("name"),
                "difficulty_raw": raw,
                "difficulty": difficulty,
            }
        )
    try:
        await db.upsert_problems(pool, payload)
        logger.info("Problems synced: %d", len(payload))
    except Exception:
        logger.exception("failed to upsert problems")


async def ensure_color_roles(guild: discord.Guild) -> None:
    if not pool:
        return
    stored = await db.get_role_colors(pool, guild.id)
    for key, name in ROLE_LABELS.items():
        role_id = stored.get(key)
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            try:
                role = await guild.create_role(name=name, colour=discord.Colour(COLOR_VALUES[key]))
            except discord.Forbidden:
                logger.warning("missing permissions to create role %s", name)
                continue
        if role:
            await db.store_role_color(pool, guild.id, key, role.id)


async def apply_color_role(member: discord.Member, rating: int) -> None:
    if not pool:
        return
    key = color_key(rating)
    stored = await db.get_role_colors(pool, member.guild.id)
    role_id = stored.get(key)
    if role_id is None:
        await ensure_color_roles(member.guild)
        stored = await db.get_role_colors(pool, member.guild.id)
        role_id = stored.get(key)
    if role_id is None:
        return
    target = member.guild.get_role(role_id)
    if not target:
        return
    # remove other color roles
    remove_roles = []
    for other_key, other_role_id in stored.items():
        if other_role_id == role_id:
            continue
        role = member.guild.get_role(other_role_id)
        if role and role in member.roles:
            remove_roles.append(role)
    try:
        if remove_roles:
            await member.remove_roles(*remove_roles)
        if target not in member.roles:
            await member.add_roles(target)
    except discord.Forbidden:
        logger.warning("missing permissions to update roles for %s", member.id)


async def polling_loop() -> None:
    global last_poll_at
    await bot.wait_until_ready()
    while True:
        try:
            await poll_all_users()
            last_poll_at = now_utc()
        except Exception:
            logger.exception("polling loop failed")
        interval = POLL_INTERVAL_SECONDS
        if pool and GUILD_ID:
            settings = await db.get_settings(pool, GUILD_ID)
            interval = settings.get("poll_interval_seconds", interval)
        await asyncio.sleep(interval)


async def problems_sync_loop() -> None:
    global last_problems_sync_at
    await bot.wait_until_ready()
    while True:
        try:
            await sync_problems()
            last_problems_sync_at = now_utc()
        except Exception:
            logger.exception("problem sync failed")
        await asyncio.sleep(PROBLEMS_SYNC_INTERVAL_SECONDS)


async def weekly_loop() -> None:
    await bot.wait_until_ready()
    while True:
        now = now_utc()
        next_run = next_week_start_jst(now)
        sleep_for = max(5, (next_run - now).total_seconds())
        await asyncio.sleep(sleep_for)
        try:
            await handle_weekly_reset()
        except Exception:
            logger.exception("weekly reset failed")


async def healthcheck_loop() -> None:
    await bot.wait_until_ready()
    while True:
        try:
            await send_healthcheck()
        except Exception:
            logger.exception("healthcheck failed")
        await asyncio.sleep(HEALTHCHECK_INTERVAL_SECONDS)


async def handle_weekly_reset() -> None:
    if not pool:
        return
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if not guild:
        return
    # weekly champion
    current_start = week_start_jst(now_utc())
    prev_start = current_start - timedelta(days=7)
    scores = await db.get_weekly_scores(pool, prev_start)
    if scores:
        winner_id = scores[0]["discord_id"]
        settings = await db.get_settings(pool, guild.id)
        role_weekly_id = settings.get("role_weekly_id")
        if role_weekly_id:
            role = guild.get_role(role_weekly_id)
            if role:
                try:
                    for member in role.members:
                        await member.remove_roles(role)
                    winner = guild.get_member(winner_id)
                    if winner:
                        await winner.add_roles(role)
                except discord.Forbidden:
                    logger.warning("missing permissions to update weekly role")
    await update_rank_message(guild)
    await update_all_ratings(guild)


async def update_all_ratings(guild: discord.Guild) -> None:
    global last_ratings_sync_at
    if not session or not pool:
        return
    users = await db.get_active_users(pool)
    for user in users:
        try:
            rating = await atcoder_api.fetch_user_rating(session, user["atcoder_id"])
            if rating is None:
                continue
            await db.upsert_rating(pool, user["discord_id"], rating)
            member = guild.get_member(user["discord_id"])
            if member:
                await apply_color_role(member, rating)
        except Exception:
            logger.exception("rating update failed: %s", user["atcoder_id"])
    last_ratings_sync_at = now_utc()


async def poll_all_users() -> None:
    if not session or not pool:
        return
    users = await db.get_active_users(pool)
    for user in users:
        try:
            await poll_user(user["discord_id"], user["atcoder_id"])
        except Exception:
            logger.exception("poll user failed: %s", user["atcoder_id"])


async def poll_user(discord_id: int, atcoder_id: str) -> None:
    if not session or not pool:
        return
    try:
        results = await atcoder_api.fetch_user_results(session, atcoder_id)
    except Exception:
        logger.exception("failed to fetch results: %s", atcoder_id)
        return
    state = await db.get_fetch_state(pool, discord_id)
    last_epoch = int(state.get("last_checked_epoch", 0))
    last_submission_id = state.get("last_submission_id")
    filtered = []
    for r in results:
        if r.get("result") != "AC":
            continue
        epoch = int(r.get("epoch_second", 0))
        sid = r.get("id")
        if epoch > last_epoch:
            filtered.append(r)
        elif epoch == last_epoch and last_submission_id is not None and sid and sid > last_submission_id:
            filtered.append(r)
        elif epoch == last_epoch and last_submission_id is None:
            filtered.append(r)
    filtered.sort(key=lambda x: (x.get("epoch_second", 0), x.get("id") or 0))
    if not filtered:
        return
    new_last_epoch = last_epoch
    new_last_id = last_submission_id
    for r in filtered:
        epoch = int(r.get("epoch_second", 0))
        submitted_at = datetime.fromtimestamp(epoch, tz=timezone.utc)
        processed = await handle_ac(discord_id, atcoder_id, r, submitted_at)
        if epoch > new_last_epoch:
            new_last_epoch = epoch
            new_last_id = r.get("id")
        elif epoch == new_last_epoch:
            rid = r.get("id")
            if rid is not None:
                new_last_id = max(new_last_id or 0, rid)
    await db.update_fetch_state(pool, discord_id, new_last_epoch, new_last_id)


async def handle_ac(discord_id: int, atcoder_id: str, submission: dict, submitted_at: datetime) -> bool:
    if not pool:
        return False
    problem_id = submission.get("problem_id")
    if not problem_id:
        return False
    last_ac_at = await db.get_last_ac(pool, discord_id, problem_id)
    if last_ac_at and submitted_at - last_ac_at < timedelta(days=7):
        return False
    problem = await db.get_problem(pool, problem_id)
    title = problem.get("title") if problem else problem_id
    difficulty = problem.get("difficulty") if problem else None

    rating = await db.get_rating(pool, discord_id)

    if difficulty is None:
        score_base = 150
        diff_emoji = ""
    else:
        score_base = base_score(rating, difficulty)
        diff_emoji = COLOR_EMOJI[color_key(difficulty)]
    rate_emoji = COLOR_EMOJI[color_key(rating)]

    streak_info = await db.get_streak(pool, discord_id)
    current_streak = streak_info["current_streak"]
    last_date = streak_info["last_ac_date"]
    today = to_jst(submitted_at).date()
    if last_date == today:
        new_streak = current_streak
    elif last_date == (today - timedelta(days=1)):
        new_streak = current_streak + 1
    else:
        new_streak = 1
    await db.update_streak(pool, discord_id, new_streak, today)

    mult = streak_multiplier(new_streak)
    score_final = round(score_base * mult)

    week_start = week_start_jst(submitted_at)
    await db.insert_submission(pool, discord_id, problem_id, submitted_at, score_base, mult, score_final)
    await db.add_weekly_score(pool, week_start, discord_id, score_final)
    await db.upsert_last_ac(pool, discord_id, problem_id, submitted_at)

    await maybe_update_streak_role(discord_id, new_streak)
    await send_ac_notification(discord_id, atcoder_id, title, score_final, diff_emoji, rate_emoji, difficulty, rating, new_streak)

    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild:
        await update_rank_message(guild)
    return True


async def maybe_update_streak_role(discord_id: int, streak: int) -> None:
    if not pool or not GUILD_ID:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    settings = await db.get_settings(pool, guild.id)
    role_id = settings.get("role_streak_id")
    if not role_id:
        return
    role = guild.get_role(role_id)
    if not role:
        return
    member = guild.get_member(discord_id)
    if not member:
        return
    try:
        if streak >= 7 and role not in member.roles:
            await member.add_roles(role)
        if streak < 7 and role in member.roles:
            await member.remove_roles(role)
    except discord.Forbidden:
        logger.warning("missing permissions to update streak role")


def pick_template(score: int) -> str:
    if score < 200:
        key = "low"
    elif score < 300:
        key = "mid"
    elif score < 400:
        key = "high"
    else:
        key = "top"
    return random.choice(NOTIFY_TEMPLATES[key])


async def send_ac_notification(
    discord_id: int,
    atcoder_id: str,
    title: str,
    score: int,
    diff_emoji: str,
    rate_emoji: str,
    difficulty: int | None,
    rating: int,
    streak: int,
) -> None:
    if not pool or not GUILD_ID:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    settings = await db.get_settings(pool, guild.id)
    notify_channel_id = settings.get("notify_channel_id")
    if not notify_channel_id:
        return
    channel = guild.get_channel(notify_channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(notify_channel_id)
        except discord.NotFound:
            logger.warning("notify channel not found: %s", notify_channel_id)
            return
        except discord.Forbidden:
            logger.warning("missing permissions to fetch notify channel")
            return
    if not isinstance(channel, discord.TextChannel):
        return

    template = pick_template(score)
    content = template.format(
        user=f"<@{discord_id}>",
        score=score,
        diff_emoji=diff_emoji,
        rate_emoji=rate_emoji,
    )

    embed = discord.Embed(title=title)
    if difficulty is not None:
        embed.color = COLOR_VALUES[color_key(difficulty)]

    ai_enabled = settings.get("ai_enabled", AI_ENABLED)
    ai_prob = settings.get("ai_probability", AI_PROBABILITY)
    if ai_enabled and random.randint(1, 100) <= ai_prob:
        week_start = week_start_jst(now_utc())
        weekly_score = await db.get_weekly_score(pool, week_start, discord_id)
        prompt = (
            "目的: AtCoderのAC通知に添える短い一言を作る。\n"
            "条件: 日本語1文・25〜60文字・絵文字1つ以上・ポジティブ。\n"
            "例:\n"
            " - ナイスAC！勢いがあるね🔥\n"
            " - 難問突破おめでとう！✨\n"
            " - いい積み上げ、継続が力💪\n"
            f"ユーザー:{atcoder_id}\n"
            f"問題:{title}\n"
            f"増加スコア:{score}\n"
            f"現在週スコア:{weekly_score}\n"
            f"difficulty:{difficulty}\n"
            f"rating:{rating}\n"
            f"streak:{streak}\n"
            "この状況に合う一言を作成。"
        )
        ai_text = await generate_message(prompt)
        if ai_text:
            content = f"{content}\n{ai_text}"

    try:
        await channel.send(content=content, embed=embed)
    except discord.Forbidden:
        logger.warning("missing permissions to send notification")


async def update_rank_message(guild: discord.Guild) -> None:
    if not pool:
        return
    settings = await db.get_settings(pool, guild.id)
    rank_channel_id = settings.get("rank_channel_id")
    if not rank_channel_id:
        return
    channel = guild.get_channel(rank_channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(rank_channel_id)
        except discord.NotFound:
            logger.warning("rank channel not found: %s", rank_channel_id)
            return
        except discord.Forbidden:
            logger.warning("missing permissions to fetch rank channel")
            return
    if not isinstance(channel, discord.TextChannel):
        return

    week_start = week_start_jst(now_utc())
    week_start_jst_str = to_jst(week_start).strftime("%Y-%m-%d %H:%M")
    scores = await db.get_weekly_scores(pool, week_start)

    lines = [f"週間ランキング（開始: {week_start_jst_str} JST）"]
    if not scores:
        lines.append("まだスコアがありません")
    else:
        for i, row in enumerate(scores, start=1):
            member = guild.get_member(row["discord_id"])
            name = member.display_name if member else f"<@{row['discord_id']}>"
            lines.append(f"{i}. {name} {row['score']}")
    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[:1900] + "\n...（省略）"

    message_id = settings.get("rank_message_id")
    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(content=content)
            return
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning("missing permissions to edit rank message")
            return
    try:
        msg = await channel.send(content)
    except discord.Forbidden:
        logger.warning("missing permissions to send rank message")
        return
    try:
        await msg.pin(reason="Ranking message")
    except discord.Forbidden:
        pass
    await db.update_setting(pool, guild.id, "rank_message_id", msg.id)


async def send_healthcheck() -> None:
    if not pool or not GUILD_ID:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    settings = await db.get_settings(pool, guild.id)
    health_channel_id = settings.get("health_channel_id")
    if not health_channel_id:
        return
    channel = guild.get_channel(health_channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(health_channel_id)
        except (discord.NotFound, discord.Forbidden):
            logger.warning("health channel not available: %s", health_channel_id)
            return
    if not isinstance(channel, discord.TextChannel):
        return

    active_users = await db.get_active_users(pool)
    now = now_utc()
    uptime = now - started_at
    uptime_hours = int(uptime.total_seconds() // 3600)
    last_poll = to_jst(last_poll_at).strftime("%m-%d %H:%M") if last_poll_at else "未実行"
    last_prob = to_jst(last_problems_sync_at).strftime("%m-%d %H:%M") if last_problems_sync_at else "未実行"
    last_rate = to_jst(last_ratings_sync_at).strftime("%m-%d %H:%M") if last_ratings_sync_at else "未実行"
    now_str = to_jst(now).strftime("%Y-%m-%d %H:%M")

    content = (
        f"🩺 稼働中 {now_str} JST\n"
        f"稼働時間: {uptime_hours}h / 登録ユーザー: {len(active_users)}\n"
        f"最終ポーリング: {last_poll} / 問題同期: {last_prob} / レート更新: {last_rate}"
    )
    try:
        await channel.send(content)
    except discord.Forbidden:
        logger.warning("missing permissions to send healthcheck")


@bot.tree.command(name="register")
@app_commands.describe(atcoder_id="AtCoder ID", user="代理登録するユーザー")
async def register(interaction: discord.Interaction, atcoder_id: str, user: discord.Member | None = None) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    target = user or interaction.user
    if user and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ代理登録できます", ephemeral=True)
        return
    await db.upsert_user(pool, target.id, atcoder_id)
    await interaction.response.send_message(f"登録しました: {target.mention} -> {atcoder_id}")
    if GUILD_ID:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            rating = await atcoder_api.fetch_user_rating(session, atcoder_id)
            if rating is not None:
                await db.upsert_rating(pool, target.id, rating)
                member = guild.get_member(target.id)
                if member:
                    await apply_color_role(member, rating)


@bot.tree.command(name="unregister")
@app_commands.describe(user="代理解除するユーザー")
async def unregister(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    target = user or interaction.user
    if user and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ代理解除できます", ephemeral=True)
        return
    await db.deactivate_user(pool, target.id)
    await interaction.response.send_message(f"解除しました: {target.mention}")


@bot.tree.command(name="set_notify_channel")
async def set_notify_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ設定できます", ephemeral=True)
        return
    await db.update_setting(pool, interaction.guild_id, "notify_channel_id", channel.id)
    await interaction.response.send_message(f"通知チャンネルを設定しました: {channel.mention}")


@bot.tree.command(name="set_rank_channel")
async def set_rank_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ設定できます", ephemeral=True)
        return
    await db.update_setting(pool, interaction.guild_id, "rank_channel_id", channel.id)
    await interaction.response.send_message(f"ランキングチャンネルを設定しました: {channel.mention}")
    guild = interaction.guild
    if guild:
        await update_rank_message(guild)


@bot.tree.command(name="set_health_channel")
async def set_health_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ設定できます", ephemeral=True)
        return
    await db.update_setting(pool, interaction.guild_id, "health_channel_id", channel.id)
    await interaction.response.send_message(f"ヘルスチェックチャンネルを設定しました: {channel.mention}")


@bot.tree.command(name="set_roles")
async def set_roles(
    interaction: discord.Interaction,
    weekly_role: discord.Role,
    streak_role: discord.Role,
) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ設定できます", ephemeral=True)
        return
    await db.update_setting(pool, interaction.guild_id, "role_weekly_id", weekly_role.id)
    await db.update_setting(pool, interaction.guild_id, "role_streak_id", streak_role.id)
    await interaction.response.send_message("ロールを設定しました")


@bot.tree.command(name="set_ai")
async def set_ai(
    interaction: discord.Interaction,
    enabled: bool,
    probability: int,
) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ設定できます", ephemeral=True)
        return
    await db.update_setting(pool, interaction.guild_id, "ai_enabled", enabled)
    await db.update_setting(pool, interaction.guild_id, "ai_probability", probability)
    await interaction.response.send_message("AI設定を更新しました")


@bot.tree.command(name="ranking")
async def ranking(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        return
    await update_rank_message(interaction.guild)
    await interaction.response.send_message("ランキングを更新しました", ephemeral=True)


@bot.tree.command(name="profile")
async def profile(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    target = user or interaction.user
    rating = await db.get_rating(pool, target.id)
    streak = await db.get_streak(pool, target.id)
    await interaction.response.send_message(
        f"{target.mention}\nレート: {rating}\nストリーク: {streak['current_streak']}日",
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
