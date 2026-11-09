import time

from cli_agent.llm import RateLimiter, _server_retry_delay, is_daily_quota_error


class _FakeQuotaError(Exception):
    pass


PER_MINUTE_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current "
    "quota', 'details': [{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier', "
    "'quotaValue': '15'}], 'retryDelay': '11s'}}"
)
PER_DAY_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current "
    "quota', 'details': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
    "'quotaValue': '20'}], 'retryDelay': '39s'}}"
)


def test_a_per_minute_429_is_worth_retrying():
    assert not is_daily_quota_error(_FakeQuotaError(PER_MINUTE_429))


def test_a_per_day_429_is_not_worth_retrying():
    # The server still sends a retryDelay. It is lying: the quota resets at midnight,
    # not in 39 seconds. Retrying it burns twenty minutes and writes zeros.
    assert is_daily_quota_error(_FakeQuotaError(PER_DAY_429))
    assert _server_retry_delay(_FakeQuotaError(PER_DAY_429)) == 39.0


def test_an_unrelated_error_is_not_a_daily_quota_error():
    assert not is_daily_quota_error(_FakeQuotaError("503 UNAVAILABLE high demand"))


def test_server_retry_delay_from_structured_payload():
    exc = _FakeQuotaError("429 RESOURCE_EXHAUSTED ... 'retryDelay': '47s'}]}}")
    assert _server_retry_delay(exc) == 47.0


def test_server_retry_delay_from_message():
    exc = _FakeQuotaError("Quota exceeded ... Please retry in 11.227696977s.")
    assert _server_retry_delay(exc) == 11.227696977


def test_server_retry_delay_is_capped():
    exc = _FakeQuotaError("'retryDelay': '3600s'")
    assert _server_retry_delay(exc) == 90.0


def test_no_retry_delay_in_a_plain_error():
    assert _server_retry_delay(_FakeQuotaError("500 internal")) is None


def test_rate_limiter_paces_requests():
    limiter = RateLimiter(requests_per_minute=600)  # 100 ms apart
    started = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    elapsed = time.monotonic() - started
    # First acquire is free; the next two each wait ~100 ms.
    assert 0.15 <= elapsed <= 0.45


def test_rate_limiter_disabled_when_rpm_is_zero():
    limiter = RateLimiter(requests_per_minute=0)
    started = time.monotonic()
    for _ in range(50):
        limiter.acquire()
    assert time.monotonic() - started < 0.05
