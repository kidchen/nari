"""Thin wrapper around Google AI Studio (Gemini) for short summaries/extraction.

The free tier regularly returns 503 (model overloaded) or 429 (quota), so callers pass a
list of models and we fall through them. Returns None if all fail; callers degrade gracefully.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)
TRANSIENT = {429, 500, 503, 504}


def summarize(api_key: str, model: str | list[str], prompt: str, retry_delay: float = 3.0,
              timeout_seconds: float = 45) -> str | None:
    if not api_key:
        return None
    from google import genai
    from google.genai import errors, types

    models = [model] if isinstance(model, str) else list(model)
    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000),
                                                         retry_options=types.HttpRetryOptions(attempts=1)))
    for i, m in enumerate(models):
        try:
            text = (client.models.generate_content(model=m, contents=prompt).text or "").strip()
            if text:
                log.info("Gemini answered with %s", m)
                return text
        except errors.APIError as e:
            log.warning("Gemini %s failed: %s %s", m, e.code, (e.message or "")[:120])
            if e.code not in TRANSIENT and e.code != 404:
                return None
            if i + 1 < len(models):
                time.sleep(retry_delay)
        except Exception as e:  # noqa: BLE001 — network errors etc. must not kill a job
            log.warning("Gemini %s failed: %s", m, e)
    return None
