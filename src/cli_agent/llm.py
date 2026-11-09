"""Thin wrapper around the Google Gemini API.

Everything the prompt experiment depends on is pinned here: temperature 0 so two
runs of the same prompt are comparable, and thinking disabled so a low
max_output_tokens does not silently produce an empty answer (gemini-2.5-*
spends output tokens on its thinking budget before it writes a single character).
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from .config import settings

try:  # the SDK moved this around between versions
    from google.genai import errors as genai_errors

    _APIError: type[Exception] = genai_errors.APIError
except Exception:  # pragma: no cover
    _APIError = Exception


class LLMError(RuntimeError):
    """Any failure while talking to the model."""


class LLMConfigError(LLMError):
    """Missing or invalid configuration (usually: no API key)."""


class LLMQuotaExhausted(LLMError):
    """The per-day free-tier quota is gone. Waiting will not help.

    Separated from LLMError on purpose. A per-minute 429 is worth retrying; a
    per-day 429 is not, and retrying it turns a fast failure into twenty minutes
    of backoff followed by a results directory half-filled with zeros.
    """


@dataclass
class LLMResponse:
    text: str
    latency_ms: int
    model: str
    finish_reason: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1


_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 6
_MAX_BACKOFF_SEC = 90.0


class RateLimiter:
    """Process-wide request pacer, shared by every GeminiLLM instance.

    The free tier allows a fixed number of requests per minute *per model*. The
    evaluation harness fans 140 calls across a thread pool, so without pacing the
    run is mostly 429s - and a 429 scores 0, which silently poisons the very
    metrics the experiment exists to produce. Pacing is not an optimisation here;
    it is what makes the numbers mean anything.
    """

    def __init__(self, requests_per_minute: int) -> None:
        self._interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait:
            time.sleep(wait)


_limiter = RateLimiter(settings.gemini_rpm)


def set_rate_limit(requests_per_minute: int) -> None:
    global _limiter
    _limiter = RateLimiter(requests_per_minute)


_RETRY_DELAY_PATTERNS = (
    re.compile(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'"),
    re.compile(r"retry in (\d+(?:\.\d+)?)s"),
)


def _server_retry_delay(exc: Exception) -> float | None:
    """Gemini tells us exactly how long to wait. Guessing is strictly worse."""
    text = str(exc)
    for pattern in _RETRY_DELAY_PATTERNS:
        match = pattern.search(text)
        if match:
            return min(float(match.group(1)), _MAX_BACKOFF_SEC)
    return None


_DAILY_QUOTA = re.compile(r"quotaId':\s*'[^']*PerDay[^']*'")


def is_daily_quota_error(exc: Exception) -> bool:
    """A 429 that says `...RequestsPerDay...`. It will not clear before midnight."""
    return bool(_DAILY_QUOTA.search(str(exc)))


@dataclass
class GeminiLLM:
    api_key: str = field(default_factory=lambda: settings.gemini_api_key)
    model: str = field(default_factory=lambda: settings.gemini_model)
    temperature: float = field(default_factory=lambda: settings.gemini_temperature)
    thinking_budget: int = field(default_factory=lambda: settings.gemini_thinking_budget)
    max_output_tokens: int = field(default_factory=lambda: settings.gemini_max_output_tokens)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise LLMConfigError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and paste your key "
                "from https://aistudio.google.com/apikey"
            )
        self._client = genai.Client(api_key=self.api_key)
        # Whether this model accepts a thinking_config at all. Model families come
        # and go, so we discover it from the first 400 rather than hard-coding a
        # list of version strings that will be wrong in six months.
        self._thinking_supported = self.thinking_budget >= 0

    def _config(self, system_prompt: str) -> types.GenerateContentConfig:
        kwargs: dict = {
            "system_instruction": system_prompt,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "candidate_count": 1,
        }
        if self._thinking_supported:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=self.thinking_budget)
        return types.GenerateContentConfig(**kwargs)

    def complete(self, system_prompt: str, user_text: str) -> LLMResponse:
        started = time.perf_counter()
        last_error: Exception | None = None
        attempt = 0

        while attempt < _MAX_ATTEMPTS:
            attempt += 1
            try:
                _limiter.acquire()
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=user_text,
                    config=self._config(system_prompt),
                )
            except _APIError as exc:  # type: ignore[misc]
                code = getattr(exc, "code", None)
                if code == 400 and self._thinking_supported and "thinking" in str(exc).lower():
                    # Our config was wrong, not the server's fault. Drop the field
                    # and retry without spending one of the real attempts.
                    self._thinking_supported = False
                    attempt -= 1
                    continue
                if code == 429 and is_daily_quota_error(exc):
                    raise LLMQuotaExhausted(
                        f"The free-tier daily quota for {self.model!r} is exhausted. "
                        "Retrying will not help before the quota resets (midnight US "
                        "Pacific). Use --model with another model, or a key from a "
                        "different Google Cloud project."
                    ) from exc
                if code in _RETRYABLE_CODES and attempt < _MAX_ATTEMPTS:
                    delay = _server_retry_delay(exc) or min(2.0**attempt, 16.0)
                    time.sleep(delay + random.uniform(0, 0.5))
                    last_error = exc
                    continue
                raise LLMError(f"Gemini API error ({code}): {exc}") from exc
            except Exception as exc:  # pragma: no cover - network/transport
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(min(2.0**attempt, 16.0) + random.uniform(0, 0.5))
                    last_error = exc
                    continue
                raise LLMError(f"Gemini call failed: {exc}") from exc

            latency_ms = int((time.perf_counter() - started) * 1000)
            return _to_response(response, self.model, latency_ms, attempt)

        raise LLMError(f"Gemini call failed after {_MAX_ATTEMPTS} attempts: {last_error}")


def _to_response(response, model: str, latency_ms: int, attempts: int) -> LLMResponse:
    finish_reason = ""
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        raw_reason = getattr(candidates[0], "finish_reason", None)
        finish_reason = getattr(raw_reason, "name", None) or str(raw_reason or "")

    usage = getattr(response, "usage_metadata", None)
    text = (getattr(response, "text", None) or "").strip()

    if not text and finish_reason == "MAX_TOKENS":
        raise LLMError(
            "Gemini returned no text: the output token budget was consumed. "
            "Raise GEMINI_MAX_OUTPUT_TOKENS or set GEMINI_THINKING_BUDGET=0."
        )

    return LLMResponse(
        text=text,
        latency_ms=latency_ms,
        model=model,
        finish_reason=finish_reason,
        prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        attempts=attempts,
    )


@dataclass
class ScriptedLLM:
    """Offline stand-in used by the unit tests. Never used to produce results."""

    responses: dict[str, str]
    default: str = "ipconfig"

    def complete(self, system_prompt: str, user_text: str) -> LLMResponse:
        for needle, reply in self.responses.items():
            if needle in user_text:
                return LLMResponse(text=reply, latency_ms=0, model="scripted")
        return LLMResponse(text=self.default, latency_ms=0, model="scripted")
