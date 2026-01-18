from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from config import AI_MODEL, OPENAI_API_KEY


logger = logging.getLogger(__name__)


def _extract_text(resp) -> Optional[str]:
    if hasattr(resp, "output_text"):
        text = resp.output_text
        if text:
            return text.strip()
    output = getattr(resp, "output", None)
    if not output:
        return None
    for item in output:
        if getattr(item, "type", None) == "message":
            for part in getattr(item, "content", []):
                if getattr(part, "type", None) == "output_text":
                    text = getattr(part, "text", "")
                    if text:
                        return text.strip()
    return None


async def generate_message(prompt: str) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    try:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        resp = await client.responses.create(
            model=AI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": "Discord向けの称賛メッセージを書く。日本語1文、絵文字1つ以上、25〜60文字で返す。",
                },
                {"role": "user", "content": prompt},
            ],
        )
        return _extract_text(resp)
    except Exception:
        logger.exception("AI message generation failed")
        return None
