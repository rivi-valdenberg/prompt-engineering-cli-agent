"""The experiment runner's failure behaviour.

A results/ directory holding one fresh iteration and two stale ones is worse than
one holding neither: the trend table it produces is a comparison between two
different samples, and nothing on its face says so.
"""

from __future__ import annotations

import pytest

from cli_agent.evaluation import run_eval
from cli_agent.llm import LLMQuotaExhausted, LLMResponse


class _QuotaDeadLLM:
    """Answers a few calls, then reports the daily quota gone - like the real one."""

    def __init__(self, answers_before_death: int = 3) -> None:
        self.calls = 0
        self._budget = answers_before_death

    def complete(self, system_prompt: str, user_text: str) -> LLMResponse:
        self.calls += 1
        if self.calls > self._budget:
            raise LLMQuotaExhausted("free-tier daily quota for 'x' is exhausted")
        return LLMResponse(text="ipconfig", latency_ms=0, model="fake")


def test_a_daily_quota_error_aborts_the_run_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_eval, "GeminiLLM", lambda **kw: _QuotaDeadLLM())

    exit_code = run_eval.main(
        ["--shell", "cmd", "--workers", "1", "--rpm", "0", "--out", str(tmp_path)]
    )

    assert exit_code == 3
    assert list(tmp_path.iterdir()) == []  # not one partial CSV
    assert "daily quota" in capsys.readouterr().err


def test_a_quota_error_is_not_swallowed_into_a_zero_scored_row():
    # run_case() records ordinary LLM errors as a failed case. A quota error is not
    # an ordinary error: it means every remaining case would also score zero.
    from cli_agent.evaluation.test_cases import BY_ID

    with pytest.raises(LLMQuotaExhausted):
        run_eval.run_case(
            BY_ID["TC01"], "v1", "cmd", _QuotaDeadLLM(answers_before_death=0), use_sandbox=False
        )
