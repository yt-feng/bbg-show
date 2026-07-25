#!/usr/bin/env python3
"""Shared DeepSeek Chat Completions client for JSON-only pipeline calls."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ERROR_BODY_CHARS = 2000


class DeepSeekAPIError(RuntimeError):
    """Raised when a DeepSeek request cannot produce valid JSON."""


def get_deepseek_model() -> str:
    """Return the configured model while defaulting to the current fast V4 model."""
    return os.environ.get("DEEPSEEK_MODEL", "").strip() or DEFAULT_DEEPSEEK_MODEL


def build_json_chat_payload(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float,
) -> dict[str, Any]:
    """Build a non-thinking V4 request matching the legacy fast-chat behavior."""
    return {
        "model": get_deepseek_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }


def _http_error_message(exc: HTTPError) -> str:
    message = f"HTTP {exc.code}: {exc.reason}"
    try:
        raw_body = exc.read()
    except OSError:
        raw_body = b""
    if not raw_body:
        return message
    if isinstance(raw_body, bytes):
        detail = raw_body.decode("utf-8", errors="replace").strip()
    else:
        detail = str(raw_body).strip()
    if not detail:
        return message
    return f"{message}; response={detail[:MAX_ERROR_BODY_CHARS]}"


def _retry_delay(retry_delays: Sequence[float], attempt_index: int) -> float:
    if not retry_delays:
        return 0.0
    return max(0.0, float(retry_delays[min(attempt_index, len(retry_delays) - 1)]))


def request_deepseek_json(
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.3,
    timeout: int = 180,
    attempts: int = 3,
    retry_delays: Sequence[float] = (5.0, 10.0),
    log_prefix: str = "  ",
) -> dict[str, Any]:
    """Call DeepSeek Chat Completions and parse the JSON object in message.content."""
    max_attempts = max(1, int(attempts))
    payload = build_json_chat_payload(
        system_prompt,
        user_prompt,
        temperature=temperature,
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        DEEPSEEK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    last_error = "unknown error"

    for attempt_index in range(max_attempts):
        retryable = True
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read())
            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise TypeError("DeepSeek message.content was not a JSON object")
            return parsed
        except HTTPError as exc:
            last_error = _http_error_message(exc)
            retryable = exc.code in RETRYABLE_HTTP_STATUSES
        except (
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            last_error = str(exc)

        print(
            f"{log_prefix}DeepSeek attempt {attempt_index + 1}/{max_attempts} failed "
            f"(model={payload['model']}): {last_error}",
            flush=True,
        )
        if not retryable or attempt_index + 1 >= max_attempts:
            break
        delay = _retry_delay(retry_delays, attempt_index)
        if delay:
            time.sleep(delay)

    raise DeepSeekAPIError(
        f"DeepSeek API failed (model={payload['model']}): {last_error}"
    )
