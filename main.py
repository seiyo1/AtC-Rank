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
    AI_MODEL_CELEBRATION,
    AI_MODELS_NOTIFY,
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
    "gray": (192, 192, 192),
    "brown": (176, 140, 86),
    "green": (63, 175, 63),
    "cyan": (66, 224, 224),
    "blue": (136, 136, 255),
    "yellow": (255, 255, 86),
    "orange": (255, 184, 54),
    "red": (255, 103, 103),
}


def color_from_key(key: str) -> discord.Colour:
    r, g, b = COLOR_VALUES[key]
    return discord.Colour.from_rgb(r, g, b)


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
    try:
        if interaction.response.is_done():
            await interaction.followup.send("コマンドでエラーが発生しました", ephemeral=True)
        else:
            await interaction.response.send_message("コマンドでエラーが発生しました", ephemeral=True)
    except discord.NotFound:
        pass


async def sync_problems() -> None:
    if not session or not pool:
        return
    try:
        models = await atcoder_api.fetch_problem_models(session)
    except Exception:
        logger.exception("failed to fetch problem models")
        return
    model_map = {}
    if isinstance(models, dict):
        if "models" in models:
            models = models["models"]
        elif "data" in models:
            models = models["data"]
        else:
            # dict mapping problem_id -> difficulty or model object
            for pid, value in models.items():
                if isinstance(value, dict):
                    model_map[pid] = value.get("difficulty")
                elif isinstance(value, (int, float)):
                    model_map[pid] = value
            if model_map:
                models = []
            else:
                logger.error(
                    "unexpected problem models payload (dict keys=%s)",
                    list(models.keys())[:5],
                )
                return
    if isinstance(models, str):
        logger.error("unexpected problem models payload (string)")
        return
    if not isinstance(models, list):
        logger.error("unexpected problem models payload type: %s", type(models))
        return

    if not model_map:
        for m in models:
            if not isinstance(m, dict):
                continue
            pid = m.get("problem_id")
            if not pid:
                continue
            model_map[pid] = m.get("difficulty")
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
                role = await guild.create_role(name=name, colour=color_from_key(key))
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


async def remove_user_roles(member: discord.Member) -> None:
    if not pool:
        return
    settings = await db.get_settings(pool, member.guild.id)
    remove_roles = []

    role_weekly_id = settings.get("role_weekly_id")
    if role_weekly_id:
        role = member.guild.get_role(role_weekly_id)
        if role and role in member.roles:
            remove_roles.append(role)

    role_streak_id = settings.get("role_streak_id")
    if role_streak_id:
        role = member.guild.get_role(role_streak_id)
        if role and role in member.roles:
            remove_roles.append(role)

    stored = await db.get_role_colors(pool, member.guild.id)
    for role_id in stored.values():
        role = member.guild.get_role(role_id)
        if role and role in member.roles:
            remove_roles.append(role)

    if not remove_roles:
        return
    try:
        await member.remove_roles(*remove_roles)
    except discord.Forbidden:
        logger.warning("missing permissions to remove roles for %s", member.id)


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
                    if winner is None:
                        try:
                            winner = await guild.fetch_member(winner_id)
                        except (discord.NotFound, discord.Forbidden):
                            winner = None
                    if winner:
                        await winner.add_roles(role)
                    else:
                        logger.warning("weekly winner not found in guild: %s", winner_id)
                except discord.Forbidden:
                    logger.warning("missing permissions to update weekly role")
        else:
            logger.info("weekly role not set; skip assignment")
    else:
        logger.info("no weekly scores for %s; skip weekly role", to_jst(prev_start).strftime("%Y-%m-%d %H:%M"))
    await send_weekly_reset_message(guild, prev_start, scores, current_start, force_ai=True)
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
    state = await db.get_fetch_state(pool, discord_id)
    last_epoch = int(state.get("last_checked_epoch", 0))
    last_submission_id = state.get("last_submission_id")
    lookback_seconds = 86400
    window_start = max(0, last_epoch - lookback_seconds)
    try:
        results = await atcoder_api.fetch_user_results(session, atcoder_id, window_start)
    except Exception:
        logger.exception("failed to fetch results: %s", atcoder_id)
        return
    filtered = []
    for r in results:
        if r.get("result") != "AC":
            continue
        epoch = int(r.get("epoch_second", 0))
        sid = r.get("id")
        if epoch < window_start:
            continue
        if epoch > last_epoch:
            filtered.append(r)
        elif epoch == last_epoch and last_submission_id is not None and sid and sid > last_submission_id:
            filtered.append(r)
        elif epoch == last_epoch and last_submission_id is None:
            filtered.append(r)
        elif epoch < last_epoch:
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
    submission_id = submission.get("id")
    last_ac_at = await db.get_last_ac(pool, discord_id, problem_id)
    if last_ac_at and submitted_at - last_ac_at < timedelta(days=7):
        return False
    problem = await db.get_problem(pool, problem_id)
    title = problem.get("title") if problem else problem_id
    difficulty = problem.get("difficulty") if problem else None
    contest_id = problem.get("contest_id") if problem else None

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
    await send_ac_notification(
        discord_id,
        atcoder_id,
        title,
        problem_id,
        contest_id,
        submission_id,
        submitted_at,
        score_final,
        score_base,
        diff_emoji,
        rate_emoji,
        difficulty,
        rating,
        new_streak,
    )

    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild:
        await update_rank_message(guild)

    await check_and_send_goal_milestone(discord_id, atcoder_id)
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
    elif score < 350:
        key = "mid"
    elif score < 400:
        key = "high"
    else:
        key = "top"
    return random.choice(NOTIFY_TEMPLATES[key])


def score_marker(score: int) -> str:
    if score < 200:
        return ""
    if score < 350:
        return "🔥"
    return "💥💥"


def model_display_name(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def build_progress_bar(current: int, target: int, length: int = 10) -> str:
    if target <= 0:
        return "░" * length
    ratio = min(current / target, 1.0)
    filled = int(ratio * length)
    return "█" * filled + "░" * (length - filled)


def build_goal_embed(
    current: int,
    target: int,
    *,
    title: str = "📊 週間目標",
    status: str = "",
) -> discord.Embed:
    pct = min(int(current / target * 100), 100) if target > 0 else 0
    bar = build_progress_bar(current, target)

    if current >= target:
        color = discord.Colour.gold()
        status = "🏆 達成！"
    elif pct >= 75:
        color = discord.Colour.green()
    elif pct >= 50:
        color = discord.Colour.blue()
    else:
        color = discord.Colour.greyple()

    embed = discord.Embed(title=f"{title} {status}", color=color)
    embed.add_field(name="進捗", value=f"{current} / {target} pts", inline=True)
    embed.add_field(name="達成率", value=f"{pct}%", inline=True)
    embed.add_field(name="", value=f"`{bar}`", inline=False)
    return embed


def build_ac_embed(
    *,
    title: str,
    display_name: str,
    description: str,
    problem_id: str,
    contest_id: str | None,
    submission_id: int | None,
    submitted_at: datetime,
    score: int,
    base_score: int,
    weekly_score: int,
    streak: int,
    difficulty: int | None,
    rating: int,
    diff_emoji: str,
    rate_emoji: str,
) -> discord.Embed:
    embed = discord.Embed(title=title)
    if difficulty is not None:
        embed.color = color_from_key(color_key(difficulty))
    submission_url = None
    if contest_id and submission_id:
        submission_url = f"https://atcoder.jp/contests/{contest_id}/submissions/{submission_id}"
        embed.url = submission_url

    if difficulty is None:
        diff_text = "未設定"
    else:
        diff_text = f"{diff_emoji} {difficulty}"
    marker = score_marker(score)
    score_text = f"**+{score}**{marker}"
    embed.add_field(name="Score", value=score_text, inline=False)
    embed.add_field(name="コメント", value=description or " ", inline=False)
    embed.add_field(name="Difficulty", value=diff_text, inline=False)
    embed.add_field(name="週間累計", value=str(weekly_score), inline=True)
    embed.add_field(name="ストリーク", value=f"{streak}日", inline=True)
    embed.add_field(name="Rating", value=f"{rate_emoji} {rating}", inline=True)
    embed.set_footer(text=f"atcrank | {to_jst(submitted_at).strftime('%Y-%m-%d %H:%M')} JST")
    return embed


async def send_ac_notification(
    discord_id: int,
    atcoder_id: str,
    title: str,
    problem_id: str,
    contest_id: str | None,
    submission_id: int | None,
    submitted_at: datetime,
    score: int,
    base_score: int,
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

    display_name = atcoder_id
    template = pick_template(score)
    description = template.format(user=display_name)

    week_start = week_start_jst(now_utc())
    weekly_score = await db.get_weekly_score(pool, week_start, discord_id)

    ai_enabled = settings.get("ai_enabled", AI_ENABLED)
    ai_prob = settings.get("ai_probability", AI_PROBABILITY)
    if ai_enabled:
        roll = random.randint(1, 100)
        logger.info("AC AI roll=%s prob=%s user=%s", roll, ai_prob, atcoder_id)
    else:
        roll = None
    if ai_enabled and roll is not None and roll <= ai_prob:
        use_hard = score >= 350
        hard_rule = "「難問/難問突破/難しい」などの語は使用可。" if use_hard else "「難問/難問突破/難しい」などの語は禁止。"
        recent_msgs = await db.get_recent_notify_history(pool, limit=5)
        msg_lines = []
        for row in recent_msgs:
            msg = row.get("message_text") or ""
            if msg:
                msg_lines.append(msg)
        recent_text = "\n".join(msg_lines) if msg_lines else "なし"
        prompt = (
            "AtCoderのAC通知に添える一言を作成。\n\n"
            "<状況>\n"
            f"- ユーザー: {atcoder_id}\n"
            f"- 問題: {title}\n"
            f"- 獲得スコア: +{score}pts（高いほど難しい問題）\n"
            f"- 週間累計: {weekly_score}pts\n"
            f"- 問題難易度: {difficulty}（数値が高いほど難問）\n"
            f"- ユーザーレート: {rating}\n"
            f"- 連続AC日数: {streak}日\n"
            "- スコア帯の目安:\n"
            "  - 0〜199: 軽め/基礎\n"
            "  - 200〜349: 標準〜やや高め\n"
            "  - 350以上: 高難度/難問\n"
            "</状況>\n\n"
            "<条件>\n"
            "- 日本語1文、25〜60文字\n"
            "- 絵文字1〜2個\n"
            "- ポジティブで自然な口調\n"
            "- 状況に合わせて言及（streak長い→継続を褒める、高難度→突破を称える等）\n"
            f"- 語彙制約: {hard_rule}\n"
            "- 難易度の表現は必須ではないが、入れる場合はスコア帯の目安に従うこと\n"
            "- 直近5件の通知と被らない内容にする（焦点を変える：例=難易度/継続/スコア/ペース/達成感など）\n"
            "</条件>\n\n"
            "<例>\n"
            "- ナイスAC！勢いがあるね🔥\n"
            "- 難問突破おめでとう！実力ついてきた✨\n"
            "- 7日連続AC、習慣化できてる💪\n"
            "- 着実に積み上げてるね、いい調子👍\n"
            "</例>\n\n"
            "<直近5件の通知（重複回避の参考）>\n"
            f"{recent_text}\n"
            "</直近5件の通知>\n\n"
            "一言のみ出力（説明不要）："
        )
        ai_texts = []
        for model_name in AI_MODELS_NOTIFY:
            ai_text = await generate_message(prompt, model=model_name)
            if ai_text:
                ai_texts.append((model_name, ai_text))
                logger.info(
                    "AC AI message ok model=%s len=%s user=%s",
                    model_name,
                    len(ai_text),
                    atcoder_id,
                )
            else:
                logger.info("AC AI message empty model=%s user=%s", model_name, atcoder_id)
        if ai_texts:
            if len(ai_texts) == 1:
                description = ai_texts[0][1]
            else:
                description = "\n".join(
                    f"[{model_display_name(model)}] {text}" for model, text in ai_texts
                )

    # descriptionはメッセージ本体のみ（難易度はフィールドに表示）

    embed = build_ac_embed(
        title=title,
        display_name=display_name,
        description=description,
        problem_id=problem_id,
        contest_id=contest_id,
        submission_id=submission_id,
        submitted_at=submitted_at,
        score=score,
        base_score=base_score,
        weekly_score=weekly_score,
        streak=streak,
        difficulty=difficulty,
        rating=rating,
        diff_emoji=diff_emoji,
        rate_emoji=rate_emoji,
    )

    content = f"<@{discord_id}>がACしました🎉"
    try:
        await channel.send(content=content, embed=embed)
    except discord.Forbidden:
        logger.warning("missing permissions to send notification")
    try:
        await db.insert_notify_history(
            pool,
            discord_id=discord_id,
            atcoder_id=atcoder_id,
            problem_id=problem_id,
            difficulty=difficulty,
            rating=rating,
            score=score,
            message_text=description,
        )
    except Exception:
        logger.exception("failed to store notify history")


async def check_and_send_goal_milestone(discord_id: int, atcoder_id: str) -> None:
    if not pool or not GUILD_ID:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    week_start = week_start_jst(now_utc())
    goal = await db.get_weekly_goal(pool, discord_id, week_start)
    if not goal:
        return
    target = goal["target_score"]
    if target <= 0:
        return
    current_score = await db.get_weekly_score(pool, week_start, discord_id)
    pct = current_score / target * 100

    milestones = [
        (100, "notified_100"),
        (75, "notified_75"),
        (50, "notified_50"),
        (25, "notified_25"),
    ]
    milestone_to_send = None
    for threshold, field in milestones:
        if pct >= threshold and not goal[field]:
            milestone_to_send = threshold
            break
    if milestone_to_send is None:
        return

    await db.update_goal_notification(pool, discord_id, week_start, milestone_to_send)
    await send_goal_milestone_notification(guild, discord_id, atcoder_id, current_score, target, milestone_to_send)


async def send_goal_milestone_notification(
    guild: discord.Guild,
    discord_id: int,
    atcoder_id: str,
    current_score: int,
    target_score: int,
    milestone: int,
) -> None:
    if not pool:
        return
    settings = await db.get_settings(pool, guild.id)
    notify_channel_id = settings.get("notify_channel_id")
    if not notify_channel_id:
        return
    channel = guild.get_channel(notify_channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(notify_channel_id)
        except (discord.NotFound, discord.Forbidden):
            return
    if not isinstance(channel, discord.TextChannel):
        return

    if milestone == 100:
        ai_comment = None
        ai_enabled = settings.get("ai_enabled", AI_ENABLED)
        if ai_enabled:
            prompt = (
                "週間目標達成のお祝いメッセージを作成。\n\n"
                "<状況>\n"
                f"- ユーザー: {atcoder_id}\n"
                f"- 目標: {target_score}pts\n"
                f"- 達成スコア: {current_score}pts\n"
                "</状況>\n\n"
                "<条件>\n"
                "- 日本語2〜3文、60〜120文字程度\n"
                "- 絵文字2〜3個\n"
                "- 達成を盛大に称え、ユーモアや個性を交えて\n"
                "- 次への意欲も促す\n"
                "</条件>\n\n"
                "<例>\n"
                "- 目標達成おめでとう！🎉 自分で決めた目標をクリアするの、最高にかっこいい。来週もその調子で攻めていこう💪\n"
                "- やりましたね！✨ コツコツ積み上げた努力が実を結んだ瞬間。この勢いで次の目標も粉砕しちゃおう🔥\n"
                "</例>\n\n"
                "メッセージのみ出力："
            )
            ai_comment = await generate_message(
                prompt,
                system_prompt="週間目標達成のお祝いメッセージを書く。日本語2〜3文、絵文字2〜3個、60〜120文字程度で返す。ユーモアを交えて。",
                model=AI_MODEL_CELEBRATION,
            )
            if ai_comment:
                logger.info("Goal AI message ok len=%s user=%s", len(ai_comment), atcoder_id)

        embed = build_goal_embed(current_score, target_score, title="🏆 週間目標達成！")
        if ai_comment:
            embed.add_field(name="", value=ai_comment, inline=False)
        content = f"<@{discord_id}>"
    else:
        embed = build_goal_embed(current_score, target_score, title=f"📊 週間目標 {milestone}% 到達！")
        content = f"<@{discord_id}>"

    try:
        await channel.send(content=content, embed=embed)
    except discord.Forbidden:
        logger.warning("missing permissions to send goal milestone notification")


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
    embed = await build_rank_embed(guild)

    message_id = settings.get("rank_message_id")
    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(content="", embed=embed)
            return
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning("missing permissions to edit rank message")
            return
    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        logger.warning("missing permissions to send rank message")
        return
    try:
        await msg.pin(reason="Ranking message")
    except discord.Forbidden:
        pass
    await db.update_setting(pool, guild.id, "rank_message_id", msg.id)


def format_rank_name(guild: discord.Guild, row: dict) -> str:
    if "name" in row and row["name"]:
        label = row["name"]
        return label if len(label) <= 24 else label[:21] + "..."
    atcoder_id = row.get("atcoder_id") or "unknown"
    user_id = row.get("discord_id")
    if not user_id:
        return atcoder_id if len(atcoder_id) <= 24 else atcoder_id[:21] + "..."
    member = guild.get_member(user_id)
    if not member:
        return atcoder_id if len(atcoder_id) <= 24 else atcoder_id[:21] + "..."
    display = member.display_name
    label = f"{atcoder_id} ({display})"
    return label if len(label) <= 24 else label[:21] + "..."


async def build_rank_embed(
    guild: discord.Guild,
    scores_override: list[dict] | None = None,
    *,
    week_start: datetime | None = None,
    as_of: datetime | None = None,
) -> discord.Embed:
    week_start = week_start or week_start_jst(now_utc())
    week_end = week_start + timedelta(days=7)
    as_of = as_of or now_utc()
    week_start_jst_str = to_jst(week_start).strftime("%Y-%m-%d %H:%M")
    week_end_jst_str = to_jst(week_end).strftime("%Y-%m-%d %H:%M")
    updated_jst_str = to_jst(as_of).strftime("%Y-%m-%d %H:%M")
    scores = scores_override or await db.get_weekly_scores(pool, week_start)

    embed = discord.Embed(
        title="🏆 週間ランキング",
        color=discord.Colour.gold(),
    )

    header = (
        f"期間: {week_start_jst_str} JST 〜 {week_end_jst_str} JST\n"
        f"更新: {updated_jst_str} JST\n"
        f"参加: {len(scores)}人"
    )

    if not scores:
        embed.description = header + "\n\n" + "まだスコアがありません"
        return embed

    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    score_width = max(2, max(len(str(row["score"])) for row in scores))
    lines = []
    for i, row in enumerate(scores, start=1):
        prefix = medal.get(i, str(i))
        score_str = str(row["score"]).rjust(score_width)
        score_str = score_str.replace(" ", "\u00A0")
        lines.append(f"{prefix} **{score_str}** - {format_rank_name(guild, row)}")
    body = "\n".join(lines)
    if len(body) > 900:
        body = body[:890] + "\n...（省略）"
    embed.description = header + "\n\n" + body
    return embed


async def send_weekly_reset_message(
    guild: discord.Guild,
    week_start: datetime,
    scores: list[dict],
    reset_time: datetime,
    *,
    force_ai: bool = False,
    channel_override: discord.TextChannel | None = None,
    mention_everyone: bool = True,
) -> None:
    if not pool:
        return
    settings = await db.get_settings(pool, guild.id)
    if channel_override is None:
        notify_channel_id = settings.get("notify_channel_id")
        if not notify_channel_id:
            return
        channel = guild.get_channel(notify_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(notify_channel_id)
            except (discord.NotFound, discord.Forbidden):
                logger.warning("notify channel not available")
                return
        if not isinstance(channel, discord.TextChannel):
            return
    else:
        channel = channel_override

    reset_str = to_jst(reset_time).strftime("%Y-%m-%d %H:%M:%S")
    total_users = len(scores)
    ai_text = None
    lines = [
        "@everyone" if mention_everyone else None,
        "週間ランキングのリセットが完了しました！",
        "先週の確定ランキングはこちら👇",
        "一週間お疲れさまでした。今週も一緒に頑張りましょう💪",
    ]
    lines = [line for line in lines if line]

    ai_enabled = settings.get("ai_enabled", AI_ENABLED)
    ai_prob = settings.get("ai_probability", AI_PROBABILITY)
    if force_ai or (ai_enabled and random.randint(1, 100) <= ai_prob):
        prev_start = week_start - timedelta(days=7)
        prev_scores = await db.get_weekly_scores(pool, prev_start)
        prev_map = {row["discord_id"]: row["score"] for row in prev_scores if row.get("discord_id") is not None}

        top_lines = []
        for i, row in enumerate(scores[:3], start=1):
            name = row.get("atcoder_id") or "unknown"
            top_lines.append(f"{i}:{name}:{row['score']}")

        repeated = []
        prev_top = {row["discord_id"] for row in prev_scores[:3] if row.get("discord_id") is not None}
        for row in scores[:3]:
            discord_id = row.get("discord_id")
            if discord_id is not None and discord_id in prev_top:
                repeated.append(row.get("atcoder_id") or "unknown")

        deltas = []
        for row in scores:
            discord_id = row.get("discord_id")
            if discord_id is None:
                continue
            prev_score = prev_map.get(discord_id)
            if prev_score is not None:
                delta = row["score"] - prev_score
                if delta != 0:
                    deltas.append((delta, row))
        deltas.sort(key=lambda x: x[0], reverse=True)
        delta_lines = []
        for delta, row in deltas[:3]:
            name = row.get("atcoder_id") or "unknown"
            sign = "+" if delta > 0 else ""
            delta_lines.append(f"{name}:{sign}{delta}")

        recent_reports = await db.get_recent_weekly_reports(pool, limit=5)
        report_blocks = []
        for report in recent_reports:
            week_label = report.get("week_start") or "unknown"
            text = report.get("ai_comment") or ""
            if text:
                report_blocks.append(f"[{week_label}] {text}")
        recent_text = "\n".join(report_blocks) if report_blocks else "なし"

        # 過去5週のランキング結果を取得
        past_rankings = []
        for i in range(1, 6):
            past_week = week_start - timedelta(days=7 * i)
            past_scores = await db.get_weekly_scores(pool, past_week)
            if past_scores:
                past_top = [f"{row.get('atcoder_id') or 'unknown'}:{row['score']}" for row in past_scores[:3]]
                week_label = to_jst(past_week).strftime("%m/%d")
                past_rankings.append(f"[{week_label}] {', '.join(past_top)}")
        past_rankings_text = "\n".join(past_rankings) if past_rankings else "なし"

        prompt = (
            "週間ランキングリセットに添えるコメントを作成。\n\n"
            "<今週の結果>\n"
            f"- 参加人数: {total_users}人\n"
            f"- 上位3名: {', '.join(top_lines) if top_lines else 'なし'}\n"
            f"- 2週連続で上位3入り: {', '.join(repeated) if repeated else 'なし'}\n"
            f"- 前週からの伸び: {', '.join(delta_lines) if delta_lines else 'なし'}\n"
            "</今週の結果>\n\n"
            "<過去5週のランキング>\n"
            f"{past_rankings_text}\n"
            "</過去5週のランキング>\n\n"
            "<過去のコメント（重複を避ける参考）>\n"
            f"{recent_text}\n"
            "</過去のコメント>\n\n"
            "<条件>\n"
            "- 日本語2〜3文、60〜120文字程度\n"
            "- 絵文字2〜3個\n"
            "- 一週間の労いと来週への応援\n"
            "- ユーモアや個性を交えて\n"
            "- 過去と被らない表現で\n"
            "- 上位者や伸びた人に言及してもよいし、全体を労うだけでもよい\n"
            "</条件>\n\n"
            "メッセージのみ出力："
        )
        ai_text = await generate_message(
            prompt,
            system_prompt="週間ランキングの労いコメントを書く。日本語2〜3文、絵文字2〜3個、60〜120文字程度で返す。ユーモアを交えて。",
            model=AI_MODEL_CELEBRATION,
        )
        if ai_text:
            lines.append(f"コメント: {ai_text}")

    lines.append("【先週の確定ランキング】")
    lines.append(f"参加: {total_users}人 | リセット: {reset_str} JST")
    report_text = "\n".join(lines)
    if channel_override is None:
        await db.upsert_weekly_report(pool, week_start, reset_time, report_text, ai_text if ai_text else None)

    embed = await build_rank_embed(
        guild,
        scores_override=scores,
        week_start=week_start,
        as_of=reset_time,
    )
    await channel.send(report_text, embed=embed)


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
    normalized = atcoder_id.strip()
    await db.upsert_user(pool, target.id, normalized)
    await interaction.response.send_message(f"登録しました: {target.mention} -> {normalized}")
    if GUILD_ID:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            rating = await atcoder_api.fetch_user_rating(session, normalized)
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
    if interaction.guild:
        member = interaction.guild.get_member(target.id)
        if member:
            await remove_user_roles(member)
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


@bot.tree.command(name="debug_notify")
async def debug_notify(interaction: discord.Interaction) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます", ephemeral=True)
        return
    if not interaction.guild or not interaction.channel:
        return
    await interaction.response.defer(ephemeral=True)
    display_name = "aisn"
    score = 320
    weekly_score = 1280
    streak = 3
    difficulty = 1200
    rating = 1500
    diff_emoji = COLOR_EMOJI[color_key(difficulty)]
    rate_emoji = COLOR_EMOJI[color_key(rating)]
    template = pick_template(score)
    description = template.format(user=display_name)
    base_score = 278
    embed = build_ac_embed(
        title="ABC999 A Sample",
        display_name=display_name,
        description=description,
        problem_id="abc999_a",
        contest_id="abc999",
        submission_id=12345678,
        submitted_at=now_utc(),
        score=score,
        base_score=base_score,
        weekly_score=weekly_score,
        streak=streak,
        difficulty=difficulty,
        rating=rating,
        diff_emoji=diff_emoji,
        rate_emoji=rate_emoji,
    )
    await interaction.channel.send(content=interaction.user.mention, embed=embed)
    await interaction.followup.send("通知プレビューを送信しました", ephemeral=True)


@bot.tree.command(name="debug_notify_ai")
async def debug_notify_ai(interaction: discord.Interaction) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます", ephemeral=True)
        return
    if not interaction.guild or not interaction.channel:
        return

    await interaction.response.defer(ephemeral=True)
    display_name = "aisn"
    atcoder_id = "aisn"
    score = 320
    weekly_score = 1280
    streak = 3
    difficulty = 1200
    rating = 1500
    diff_emoji = COLOR_EMOJI[color_key(difficulty)]
    rate_emoji = COLOR_EMOJI[color_key(rating)]
    template = pick_template(score)
    description = template.format(user=display_name)
    use_hard = score >= 350
    hard_rule = "「難問/難問突破/難しい」などの語は使用可。" if use_hard else "「難問/難問突破/難しい」などの語は禁止。"
    recent_msgs = await db.get_recent_notify_history(pool, limit=5)
    msg_lines = []
    for row in recent_msgs:
        msg = row.get("message_text") or ""
        if msg:
            msg_lines.append(msg)
    recent_text = "\n".join(msg_lines) if msg_lines else "なし"
    prompt = (
        "AtCoderのAC通知に添える一言を作成。\n\n"
        "<状況>\n"
        f"- ユーザー: {atcoder_id}\n"
        "- 問題: ABC999 A Sample\n"
        f"- 獲得スコア: +{score}pts（高いほど難しい問題）\n"
        f"- 週間累計: {weekly_score}pts\n"
        f"- 問題難易度: {difficulty}（数値が高いほど難問）\n"
        f"- ユーザーレート: {rating}\n"
        f"- 連続AC日数: {streak}日\n"
        "- スコア帯の目安:\n"
        "  - 0〜199: 軽め/基礎\n"
        "  - 200〜349: 標準〜やや高め\n"
        "  - 350以上: 高難度/難問\n"
        "</状況>\n\n"
        "<条件>\n"
        "- 日本語1文、25〜60文字\n"
        "- 絵文字1〜2個\n"
        "- ポジティブで自然な口調\n"
        "- 状況に合わせて言及（streak長い→継続を褒める、高難度→突破を称える等）\n"
        f"- 語彙制約: {hard_rule}\n"
        "- 難易度の表現は必須ではないが、入れる場合はスコア帯の目安に従うこと\n"
        "- 直近5件の通知と被らない内容にする（焦点を変える：例=難易度/継続/スコア/ペース/達成感など）\n"
        "</条件>\n\n"
        "<例>\n"
        "- ナイスAC！勢いがあるね🔥\n"
        "- 難問突破おめでとう！実力ついてきた✨\n"
        "- 7日連続AC、習慣化できてる💪\n"
        "- 着実に積み上げてるね、いい調子👍\n"
        "</例>\n\n"
        "<直近5件の通知（重複回避の参考）>\n"
        f"{recent_text}\n"
        "</直近5件の通知>\n\n"
        "一言のみ出力（説明不要）："
    )
    ai_texts = []
    for model_name in AI_MODELS_NOTIFY:
        ai_text = await generate_message(prompt, model=model_name)
        if ai_text:
            ai_texts.append((model_name, ai_text))
    if ai_texts:
        if len(ai_texts) == 1:
            description = ai_texts[0][1]
        else:
            description = "\n".join(
                f"[{model_display_name(model)}] {text}" for model, text in ai_texts
            )
    base_score = 278
    embed = build_ac_embed(
        title="ABC999 A Sample",
        display_name=display_name,
        description=description,
        problem_id="abc999_a",
        contest_id="abc999",
        submission_id=12345678,
        submitted_at=now_utc(),
        score=score,
        base_score=base_score,
        weekly_score=weekly_score,
        streak=streak,
        difficulty=difficulty,
        rating=rating,
        diff_emoji=diff_emoji,
        rate_emoji=rate_emoji,
    )
    await interaction.channel.send(content=interaction.user.mention, embed=embed)
    await interaction.followup.send("AI通知プレビューを送信しました", ephemeral=True)


@bot.tree.command(name="debug_rank")
async def debug_rank(interaction: discord.Interaction) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます", ephemeral=True)
        return
    if not interaction.guild or not interaction.channel:
        return
    fake_scores = [
        {"name": "Alice", "score": 1820},
        {"name": "Bob", "score": 1710},
        {"name": "Carol", "score": 1590},
        {"name": "Dave", "score": 1505},
        {"name": "Erin", "score": 1430},
        {"name": "Fiona", "score": 1310},
        {"name": "Gabe", "score": 1215},
        {"name": "Hana", "score": 1150},
        {"name": "Ivan", "score": 980},
        {"name": "Jill", "score": 920},
    ]
    embed = await build_rank_embed(interaction.guild, scores_override=fake_scores)
    await interaction.channel.send(embed=embed)
    await interaction.response.send_message("ランキングプレビューを送信しました", ephemeral=True)


@bot.tree.command(name="debug_weekly_reset")
async def debug_weekly_reset(interaction: discord.Interaction) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます", ephemeral=True)
        return
    if not interaction.guild:
        return
    await interaction.response.defer(ephemeral=True)
    fake_scores = [
        {"atcoder_id": "yz_", "score": 1152},
        {"atcoder_id": "ri_ra", "score": 747},
        {"atcoder_id": "sen469", "score": 600},
        {"atcoder_id": "yuki_hitori", "score": 529},
        {"atcoder_id": "blue_island", "score": 0},
        {"atcoder_id": "carduusmille", "score": 0},
    ]
    await send_weekly_reset_message(
        interaction.guild,
        week_start_jst(now_utc()) - timedelta(days=7),
        fake_scores,
        next_week_start_jst(now_utc()),
        force_ai=False,
        channel_override=interaction.channel,
        mention_everyone=False,
    )
    await interaction.followup.send("週間リセット通知を送信しました", ephemeral=True)


@bot.tree.command(name="debug_weekly_reset_ai")
async def debug_weekly_reset_ai(interaction: discord.Interaction) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます", ephemeral=True)
        return
    if not interaction.guild:
        return
    await interaction.response.defer(ephemeral=True)
    fake_scores = [
        {"atcoder_id": "yz_", "score": 1152},
        {"atcoder_id": "ri_ra", "score": 747},
        {"atcoder_id": "sen469", "score": 600},
        {"atcoder_id": "yuki_hitori", "score": 529},
        {"atcoder_id": "blue_island", "score": 0},
        {"atcoder_id": "carduusmille", "score": 0},
    ]
    await send_weekly_reset_message(
        interaction.guild,
        week_start_jst(now_utc()) - timedelta(days=7),
        fake_scores,
        next_week_start_jst(now_utc()),
        force_ai=True,
        channel_override=interaction.channel,
        mention_everyone=False,
    )
    await interaction.followup.send("AI付き週間リセット通知を送信しました", ephemeral=True)


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


goal_group = app_commands.Group(name="goal", description="週間目標の設定")


@goal_group.command(name="set")
@app_commands.describe(score="目標スコア")
async def goal_set(interaction: discord.Interaction, score: int) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    if score <= 0:
        await interaction.response.send_message("目標スコアは1以上を指定してください", ephemeral=True)
        return
    ws = week_start_jst(now_utc())
    await db.upsert_weekly_goal(pool, interaction.user.id, ws, score)
    current_score = await db.get_weekly_score(pool, ws, interaction.user.id)
    embed = build_goal_embed(current_score, score, title="🎯 目標を設定しました")
    await interaction.response.send_message(embed=embed)


@goal_group.command(name="show")
async def goal_show(interaction: discord.Interaction) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    ws = week_start_jst(now_utc())
    goal = await db.get_weekly_goal(pool, interaction.user.id, ws)
    if not goal:
        await interaction.response.send_message("今週の目標が設定されていません。`/goal set` で設定してください", ephemeral=True)
        return
    target = goal["target_score"]
    current_score = await db.get_weekly_score(pool, ws, interaction.user.id)
    embed = build_goal_embed(current_score, target)
    await interaction.response.send_message(embed=embed)


@goal_group.command(name="clear")
async def goal_clear(interaction: discord.Interaction) -> None:
    if not pool:
        await interaction.response.send_message("DB未接続", ephemeral=True)
        return
    week_start = week_start_jst(now_utc())
    goal = await db.get_weekly_goal(pool, interaction.user.id, week_start)
    if not goal:
        await interaction.response.send_message("今週の目標が設定されていません", ephemeral=True)
        return
    await db.delete_weekly_goal(pool, interaction.user.id, week_start)
    await interaction.response.send_message("週間目標を解除しました")


bot.tree.add_command(goal_group)


# ==================== Persistent Menu ====================

class RegisterModal(discord.ui.Modal, title="AtCoder ID 登録"):
    atcoder_id = discord.ui.TextInput(
        label="AtCoder ID",
        placeholder="例: tourist",
        min_length=1,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not pool:
            await interaction.response.send_message("DB未接続", ephemeral=True)
            return
        normalized = str(self.atcoder_id).strip()
        await db.upsert_user(pool, interaction.user.id, normalized)
        await interaction.response.send_message(f"✅ 登録しました: {normalized}", ephemeral=True)
        if GUILD_ID:
            guild = bot.get_guild(GUILD_ID)
            if guild and session:
                rating = await atcoder_api.fetch_user_rating(session, normalized)
                if rating is not None:
                    await db.upsert_rating(pool, interaction.user.id, rating)
                    member = guild.get_member(interaction.user.id)
                    if member:
                        await apply_color_role(member, rating)


class GoalSetModal(discord.ui.Modal, title="週間目標を設定"):
    target_score = discord.ui.TextInput(
        label="目標スコア",
        placeholder="例: 1000",
        min_length=1,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not pool:
            await interaction.response.send_message("DB未接続", ephemeral=True)
            return
        try:
            score = int(str(self.target_score).strip())
        except ValueError:
            await interaction.response.send_message("❌ 数値を入力してください", ephemeral=True)
            return
        if score <= 0:
            await interaction.response.send_message("❌ 1以上の数値を入力してください", ephemeral=True)
            return
        ws = week_start_jst(now_utc())
        await db.upsert_weekly_goal(pool, interaction.user.id, ws, score)
        current_score = await db.get_weekly_score(pool, ws, interaction.user.id)
        embed = build_goal_embed(current_score, score, title="🎯 目標を設定しました")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ConfirmUnregisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="解除する", style=discord.ButtonStyle.danger, emoji="❌")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("DB未接続", ephemeral=True)
            return
        await db.deactivate_user(pool, interaction.user.id)
        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                await remove_user_roles(member)
        await interaction.response.edit_message(content="✅ 登録を解除しました", view=None)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ キャンセルしました", view=None)


class ConfirmGoalClearView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="解除する", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("DB未接続", ephemeral=True)
            return
        ws = week_start_jst(now_utc())
        await db.delete_weekly_goal(pool, interaction.user.id, ws)
        await interaction.response.edit_message(content="✅ 週間目標を解除しました", view=None)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ キャンセルしました", view=None)


class MenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="登録", style=discord.ButtonStyle.success, custom_id="menu:register", emoji="✅", row=0)
    async def register_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegisterModal())

    @discord.ui.button(label="登録解除", style=discord.ButtonStyle.danger, custom_id="menu:unregister", emoji="❌", row=0)
    async def unregister_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "⚠️ 登録を解除しますか？\nロールも削除されます。",
            view=ConfirmUnregisterView(),
            ephemeral=True,
        )

    @discord.ui.button(label="目標設定", style=discord.ButtonStyle.primary, custom_id="menu:goal_set", emoji="🎯", row=1)
    async def goal_set_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GoalSetModal())

    @discord.ui.button(label="目標確認", style=discord.ButtonStyle.secondary, custom_id="menu:goal_show", emoji="📊", row=1)
    async def goal_show_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("DB未接続", ephemeral=True)
            return
        ws = week_start_jst(now_utc())
        goal = await db.get_weekly_goal(pool, interaction.user.id, ws)
        if not goal:
            await interaction.response.send_message("📊 今週の目標が設定されていません", ephemeral=True)
            return
        target = goal["target_score"]
        current_score = await db.get_weekly_score(pool, ws, interaction.user.id)
        embed = build_goal_embed(current_score, target)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="目標解除", style=discord.ButtonStyle.secondary, custom_id="menu:goal_clear", emoji="🗑️", row=1)
    async def goal_clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("DB未接続", ephemeral=True)
            return
        ws = week_start_jst(now_utc())
        goal = await db.get_weekly_goal(pool, interaction.user.id, ws)
        if not goal:
            await interaction.response.send_message("📊 今週の目標が設定されていません", ephemeral=True)
            return
        target = goal["target_score"]
        await interaction.response.send_message(
            f"⚠️ 週間目標 **{target}pts** を解除しますか？",
            view=ConfirmGoalClearView(),
            ephemeral=True,
        )

    @discord.ui.button(label="プロフィール", style=discord.ButtonStyle.secondary, custom_id="menu:profile", emoji="👤", row=2)
    async def profile_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("DB未接続", ephemeral=True)
            return
        rating = await db.get_rating(pool, interaction.user.id)
        streak = await db.get_streak(pool, interaction.user.id)
        atcoder_id = await db.get_user_atcoder_id(pool, interaction.user.id)
        if not atcoder_id:
            await interaction.response.send_message("❌ 登録されていません", ephemeral=True)
            return
        await interaction.response.send_message(
            f"👤 **{atcoder_id}**\n"
            f"レート: {rating}\n"
            f"ストリーク: {streak['current_streak']}日",
            ephemeral=True,
        )


@bot.tree.command(name="menu")
async def menu_command(interaction: discord.Interaction) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます", ephemeral=True)
        return
    embed = discord.Embed(
        title="📋 AtCrank メニュー",
        description="ボタンをクリックして操作できます",
        color=discord.Colour.blue(),
    )
    embed.add_field(name="🔑 登録", value="AtCoder IDを登録・解除", inline=False)
    embed.add_field(name="🎯 週間目標", value="目標の設定・確認・解除", inline=False)
    embed.add_field(name="👤 プロフィール", value="自分の情報を確認", inline=False)
    await interaction.response.send_message(embed=embed, view=MenuView())


# Bot起動時にPersistent Viewを登録
@bot.event
async def setup_hook():
    bot.add_view(MenuView())


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
