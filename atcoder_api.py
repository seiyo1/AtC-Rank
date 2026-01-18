from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)

BASE = "https://kenkoooo.com/atcoder"


async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    retries = 3
    base_delay = 1.0
    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status in {429, 500, 502, 503, 504}:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        delay = float(retry_after)
                    else:
                        delay = base_delay * (2 ** (attempt - 1)) + random.random()
                    logger.warning("Transient HTTP %s for %s, retrying in %.1fs", resp.status, url, delay)
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if isinstance(exc, aiohttp.ClientResponseError) and exc.status in {400, 401, 403, 404}:
                raise
            if attempt == retries:
                logger.exception("HTTP failed for %s", url)
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.random()
            logger.warning("HTTP error for %s (%s). retrying in %.1fs", url, exc, delay)
            await asyncio.sleep(delay)


async def fetch_problem_models(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    url = f"{BASE}/resources/problem-models.json"
    return await fetch_json(session, url)


async def fetch_problems(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    url = f"{BASE}/resources/problems.json"
    return await fetch_json(session, url)


async def fetch_user_results(
    session: aiohttp.ClientSession,
    atcoder_id: str,
    since_epoch: int | None = None,
) -> list[dict[str, Any]]:
    raw = atcoder_id.strip()
    candidates = [raw]
    lower = raw.lower()
    if lower != raw:
        candidates.append(lower)
    from_second = since_epoch if since_epoch and since_epoch > 0 else 0

    # v3 user submissions (preferred)
    for candidate in candidates:
        url = f"{BASE}/atcoder-api/v3/user/submissions?user={candidate}&from_second={from_second}"
        try:
            return await fetch_json(session, url)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                continue
            raise

    # legacy results API fallback
    for candidate in candidates:
        url = f"{BASE}/atcoder-api/results?user={candidate}"
        try:
            return await fetch_json(session, url)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                continue
            raise

    logger.info("no submissions found via API for user: %s", atcoder_id)
    return []


async def fetch_user_rating(session: aiohttp.ClientSession, atcoder_id: str) -> int | None:
    raw = atcoder_id.strip()
    candidates = [raw]
    lower = raw.lower()
    if lower != raw:
        candidates.append(lower)
    data = None
    for candidate in candidates:
        url = f"https://atcoder.jp/users/{candidate}/history/json"
        try:
            data = await fetch_json(session, url)
            break
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                continue
            logger.exception("rating fetch failed", extra={"user": atcoder_id})
            return None
        except Exception:
            logger.exception("rating fetch failed", extra={"user": atcoder_id})
            return None
    if data is None:
        logger.info("rating API returned 404 for user: %s", atcoder_id)
        return None
    if not data:
        return 0
    last = data[-1]
    rating = last.get("NewRating")
    if rating is None:
        return 0
    return int(rating)
