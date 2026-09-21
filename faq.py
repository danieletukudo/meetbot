#!/usr/bin/env python3
"""Extract polished FAQ pairs from a meeting transcript via OpenAI."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from meeting_bot import RecallError

WHITESPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return WHITESPACE.sub(" ", (text or "").strip())


def _openai_chat(messages: list[dict[str, str]], api_key: str, model: str) -> str:
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RecallError(f"OpenAI FAQ request failed ({exc.code}): {detail or 'no body'}") from exc
    except urllib.error.URLError as exc:
        raise RecallError(f"Could not reach OpenAI: {exc.reason}") from exc

    choices = body.get("choices") or []
    if not choices:
        raise RecallError("OpenAI returned no FAQ choices.")
    content = ((choices[0].get("message") or {}).get("content")) or ""
    if not content.strip():
        raise RecallError("OpenAI returned an empty FAQ response.")
    return content


def _system_prompt(limit: int) -> str:
    return f"""You turn a meeting transcript into a clean FAQ section for a website.

Return JSON only in this shape:
{{"faqs":[{{"question":"...","answer":"..."}}]}}

Rules for each FAQ:
- question: one short, polished, standalone FAQ question — the kind you would see in a help center.
  NEVER paste the raw transcript wording. Remove greetings, filler, stutters, and repeated phrases.
  Good: "What does your company do?"
  Bad: "Hello. Please, can you tell me about your business? What do you guys. What do you guys do?"
  Good: "How much do you charge?"
  Bad: "How much you guys charge?"
- answer: one or two clear sentences, grammatically clean, based only on what was said.
  Do not invent facts. If the answer is incomplete in the transcript, say so briefly.
- Prefer useful product/business FAQs. Skip small talk and duplicated questions.
- Create at most {limit} FAQ pairs. If nothing useful was discussed, return {{"faqs":[]}}.
"""


def extract_faqs_openai(transcript_text: str, limit: int = 12) -> list[dict[str, str]]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RecallError("Missing OPENAI_API_KEY.")
    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    clipped = transcript_text.strip()
    if len(clipped) > 24000:
        clipped = clipped[:24000] + "\n…[truncated]"

    content = _openai_chat(
        [
            {
                "role": "system",
                "content": _system_prompt(limit),
            },
            {
                "role": "user",
                "content": (
                    "Rewrite this meeting into polished FAQ question/answer pairs.\n\n"
                    f"Transcript:\n\n{clipped}"
                ),
            },
        ],
        api_key=api_key,
        model=model,
    )
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RecallError("OpenAI returned invalid FAQ JSON.") from exc

    raw = data.get("faqs") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise RecallError("OpenAI FAQ JSON was missing a faqs list.")

    faqs: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = _clean(str(item.get("question") or ""))
        answer = _clean(str(item.get("answer") or ""))
        if not question or not answer:
            continue
        if not question.endswith("?"):
            question = f"{question}?"
        faqs.append({"question": question, "answer": answer, "source": "openai"})
        if len(faqs) >= limit:
            break
    return faqs


def extract_faqs(
    entries: list[dict[str, str]],
    transcript_text: str | None = None,
) -> dict[str, Any]:
    """Build polished FAQs with OpenAI. Requires OPENAI_API_KEY."""
    text = transcript_text or "\n".join(
        f"{row.get('speaker')}: {row.get('text')}" for row in entries
    )
    if not entries and not (text or "").strip():
        return {"items": [], "method": "none", "error": None}

    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return {
            "items": [],
            "method": "none",
            "error": "Add OPENAI_API_KEY to .env, then restart the server to generate polished FAQs.",
        }

    try:
        items = extract_faqs_openai(text)
        return {"items": items, "method": "openai", "error": None}
    except Exception as exc:
        return {"items": [], "method": "none", "error": str(exc)}
