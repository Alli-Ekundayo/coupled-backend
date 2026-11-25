"""Resilient Gemini LLM helper (structured JSON, plain text, and multi-turn chat).
Returns None when no GEMINI_API_KEY is set so callers can use deterministic fallbacks."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from . import config

logger = logging.getLogger(__name__)


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


def _model_candidates() -> list[str]:
    seen: list[str] = []
    for m in [config.LLM_MODEL, *config.LLM_FALLBACK_MODELS]:
        if m and m not in seen:
            seen.append(m)
    return seen


def _extract_json(txt: str) -> Any:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (txt or "").strip(), flags=re.M).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Recover if model wrapped JSON in brief prose
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", s)
        if m:
            return json.loads(m.group(1))
        raise


def _generate(
    system: str,
    contents: Any,
    *,
    json_mode: bool = True,
    max_tokens: int = 8192,
    retries_per_model: int = 2,
) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={"api_version": "v1beta", "timeout": 45000})
    cfg_kwargs: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
    }
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"
    gen_cfg = types.GenerateContentConfig(**cfg_kwargs)

    last_err: Exception | None = None
    for model_name in _model_candidates():
        for attempt in range(retries_per_model):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=gen_cfg,
                )
                txt = response.text or ""
                if txt.strip():
                    return txt
                raise ValueError(f"Empty response from Gemini model {model_name}")
            except Exception as exc:
                last_err = exc
                msg = str(exc)
                transient = any(k in msg for k in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "timeout", "EOF", "ConnectError", "ServerError"))
                logger.warning("Gemini call failed (model=%s, attempt=%d): %s", model_name, attempt + 1, exc)
                if transient and attempt + 1 < retries_per_model:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                break  # move to next fallback model
    assert last_err is not None
    raise last_err


def complete_json(system: str, user: str, max_tokens: int = 8192) -> Any:
    """Single-turn structured JSON completion. Returns None when offline."""
    if not available():
        return None
    sys_prompt = system + "\nRespond with a single valid JSON object only. No prose, no markdown fences."
    txt = _generate(sys_prompt, user, json_mode=True, max_tokens=max_tokens)
    return _extract_json(txt)


def complete_text(system: str, user: str, max_tokens: int = 2048) -> str | None:
    """Single-turn natural-language completion. Returns None when offline."""
    if not available():
        return None
    return _generate(system, user, json_mode=False, max_tokens=max_tokens).strip()


def chat_json(system: str, messages: list[dict[str, str]], max_tokens: int = 8192) -> Any:
    """Multi-turn structured JSON completion over [{'role': 'user'|'assistant', 'content': '...'}]."""
    if not available():
        return None
    sys_prompt = system + "\nRespond with a single valid JSON object only. No prose, no markdown fences."
    transcript = []
    for m in messages:
        role = "model" if m.get("role") in ("assistant", "model") else "user"
        transcript.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    txt = _generate(sys_prompt, transcript, json_mode=True, max_tokens=max_tokens)
    return _extract_json(txt)
