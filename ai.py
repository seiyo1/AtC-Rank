from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from config import AI_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL


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


async def generate_message(
    prompt: str,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    try:
        client_kwargs = {"api_key": OPENAI_API_KEY}
        if OPENAI_BASE_URL:
            client_kwargs["base_url"] = OPENAI_BASE_URL
        client = AsyncOpenAI(**client_kwargs)
        sys_prompt = system_prompt or "Discord向けの称賛メッセージを書く。日本語1文、絵文字1つ以上、25〜60文字で返す。"
        resp = await client.responses.create(
            model=model or AI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": sys_prompt,
                },
                {"role": "user", "content": prompt},
            ],
        )
        return _extract_text(resp)
    except Exception:
        logger.exception("AI message generation failed")
        return None
