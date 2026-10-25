"""Thin wrapper around Google AI Studio (Gemini) with a per-model circuit breaker.

The free tier often answers 503 (model overloaded) or 429 (rate limited). Callers pass a preferred
list of models; ModelHealth remembers recent failures (persisted, survives restarts) so a model that
keeps failing is put on a cooldown and skipped — we go straight to the one that works — and is
probed again once the cooldown expires. Returns None if every model fails; callers degrade gracefully.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)
TRANSIENT = {408, 429, 500, 503, 504}

# Cooldown policy (seconds)
COOLDOWN_503_BASE = 15 * 60          # doubles per consecutive failure ...
COOLDOWN_MAX = 6 * 3600              # ... up to 6h
COOLDOWN_429 = 120                   # per-minute rate limit: short
COOLDOWN_404 = 7 * 86400             # model retired / not available to this key


class ModelHealth:
    """Per-model failure memory. `load`/`save` persist a JSON blob (e.g. DB key-value)."""

    KEY = "llm_health"

    def __init__(self, load: Callable[[str], str | None] | None = None,
                 save: Callable[[str, str], None] | None = None, clock: Callable[[], float] = time.time):
        self._save, self.clock = save, clock
        self._lock = threading.Lock()
        raw = load(self.KEY) if load else None
        self.state: dict[str, dict] = json.loads(raw) if raw else {}

    def _persist(self) -> None:
        if self._save:
            self._save(self.KEY, json.dumps(self.state))

    def cooling(self, model: str) -> bool:
        return self.state.get(model, {}).get("until", 0) > self.clock()

    def order(self, models: list[str]) -> list[str]:
        """Healthy models in preference order, then cooling ones (soonest-recovering first) as last resort."""
        healthy = [m for m in models if not self.cooling(m)]
        cooling = sorted((m for m in models if self.cooling(m)), key=lambda m: self.state[m]["until"])
        return healthy + cooling

    STATS_DAYS = 30

    def _count(self, s: dict, outcome: str) -> None:
        """Per-day tallies ('ok' or the error code) for usage review; only the last 30 days are kept."""
        from datetime import date, timedelta
        today = date.fromtimestamp(self.clock())
        days = s.setdefault("days", {})
        day = days.setdefault(today.isoformat(), {})
        day[outcome] = day.get(outcome, 0) + 1
        cutoff = (today - timedelta(days=self.STATS_DAYS)).isoformat()
        for d in [d for d in days if d < cutoff]:
            del days[d]

    def stats(self, model: str, days: int = 7) -> dict[str, int]:
        from datetime import date, timedelta
        cutoff = (date.fromtimestamp(self.clock()) - timedelta(days=days - 1)).isoformat()
        out: dict[str, int] = {}
        for d, counts in self.state.get(model, {}).get("days", {}).items():
            if d >= cutoff:
                for k, v in counts.items():
                    out[k] = out.get(k, 0) + v
        return out

    def record_ok(self, model: str) -> None:
        with self._lock:
            s = self.state.setdefault(model, {})
            s.update(fails=0, until=0, last_ok=int(self.clock()))
            self._count(s, "ok")
            self._persist()

    def record_fail(self, model: str, code: int | None) -> None:
        with self._lock:
            s = self.state.setdefault(model, {})
            s["fails"] = s.get("fails", 0) + 1
            if code == 404:
                cool = COOLDOWN_404
            elif code == 429:
                cool = COOLDOWN_429
            else:
                cool = min(COOLDOWN_MAX, COOLDOWN_503_BASE * 2 ** (s["fails"] - 1))
            s.update(until=int(self.clock() + cool), last_err=code, last_fail=int(self.clock()))
            self._count(s, str(code or "error"))
            self._persist()

    def summary(self) -> list[str]:
        now = self.clock()
        out = []
        for m, s in sorted(self.state.items()):
            if s.get("until", 0) > now:
                mins = max(1, round((s["until"] - now) / 60))
                out.append(f"{m}: cooling down {mins} min (last error {s.get('last_err')}, {s.get('fails')} in a row)")
            else:
                ok = s.get("last_ok")
                out.append(f"{m}: healthy" + (f" (last success {round((now - ok) / 3600, 1)}h ago)" if ok else ""))
            st = self.stats(m)
            if total := sum(st.values()):
                errs = ", ".join(f"{v}× {k}" for k, v in sorted(st.items()) if k != "ok")
                out[-1] += f" · 7d: {st.get('ok', 0)}/{total} ok" + (f" ({errs})" if errs else "")
        return out or ["no calls recorded yet"]


def summarize(api_key: str, model: str | list[str], prompt: str, retry_delay: float = 2.0,
              timeout_seconds: float = 45, json_output: bool = False, attempts_per_model: int = 1,
              backoff_seconds: float = 20, health: ModelHealth | None = None,
              _client_factory: Callable | None = None, _sleep: Callable[[float], None] = time.sleep) -> str | None:
    if not api_key:
        return None
    from google import genai
    from google.genai import errors, types

    models = [model] if isinstance(model, str) else list(model)
    if health:
        models = health.order(models)
    make = _client_factory or (lambda: genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000),
                                                        retry_options=types.HttpRetryOptions(attempts=1))))
    client = make()
    cfg = types.GenerateContentConfig(response_mime_type="application/json") if json_output else None
    for i, m in enumerate(models):
        # A model already known to be failing gets one probe, not the full retry budget.
        attempts = 1 if (health and health.cooling(m)) else attempts_per_model
        code = None
        for attempt in range(1, attempts + 1):
            code = None
            try:
                text = (client.models.generate_content(model=m, contents=prompt, config=cfg).text or "").strip()
                if text:
                    log.info("Gemini answered with %s (attempt %d)", m, attempt)
                    if health:
                        health.record_ok(m)
                    return text
            except errors.APIError as e:
                code = e.code
                log.warning("Gemini %s attempt %d failed: %s %s", m, attempt, e.code, (e.message or "")[:120])
                if e.code not in TRANSIENT and e.code != 404:
                    return None                 # bad key / bad request: no point continuing
            except Exception as e:  # noqa: BLE001 — network errors/timeouts must not kill a job
                log.warning("Gemini %s attempt %d failed: %s", m, attempt, e)
            if code in (404, 429):
                break                           # gone or rate-limited: move to the next model now
            if attempt < attempts:
                _sleep(backoff_seconds)
        if health:
            health.record_fail(m, code)
        if i + 1 < len(models):
            _sleep(retry_delay)
    return None
