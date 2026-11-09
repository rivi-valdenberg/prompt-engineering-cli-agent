import pytest

from cli_agent.config import SHELLS
from cli_agent.prompts import PROMPT_VERSIONS, ordered_versions, render
from cli_agent.evaluation.test_cases import TEST_CASES, cases_for


@pytest.mark.parametrize("version", sorted(PROMPT_VERSIONS))
@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_every_prompt_renders_with_no_leftover_placeholders(version, shell):
    text = render(version, shell)
    assert "{{" not in text
    assert SHELLS[shell].shell_name in text


def test_iterations_are_ordered_and_each_declares_its_single_change():
    versions = ordered_versions()
    assert [v.order for v in versions] == list(range(1, len(versions) + 1))
    for v in versions:
        assert v.change and v.hypothesis


def test_exactly_three_iterations():
    # The assignment asks for at least three passes of the improve/measure loop.
    assert [v.id for v in ordered_versions()] == ["v1", "v2", "v3"]


def test_later_iterations_are_supersets_of_the_output_contract():
    for version in ("v2", "v3"):
        assert "OUTPUT CONTRACT" in render(version, "cmd")
    assert "OUTPUT CONTRACT" not in render("v1", "cmd")


def test_safety_policy_reaches_the_prompt_from_the_code():
    # safety.POLICY_SUMMARY is the single source of truth for both layers.
    hardened = render("v3", "cmd")
    assert "BLOCKED: <reason>" in hardened
    assert "shuts down, reboots or halts" in hardened
    assert "SAFETY POLICY" not in render("v2", "cmd")
    assert "SAFETY POLICY" not in render("v1", "cmd")


def test_each_way_of_saying_no_appears_in_the_iteration_that_introduced_it():
    contract = render("v2", "cmd")
    assert "UNSUPPORTED:" in contract  # v2 introduced the first refusal form
    assert "BLOCKED:" not in contract
    assert "CLARIFY:" not in contract

    hardened = render("v3", "cmd")
    for form in ("UNSUPPORTED:", "BLOCKED:", "CLARIFY:"):
        assert form in hardened


def test_worked_examples_and_decision_procedure_only_in_v3():
    assert "EXAMPLES" in render("v3", "bash")
    assert "ip addr show" in render("v3", "bash")
    assert "DECISION PROCEDURE" in render("v3", "bash")
    assert "EXAMPLES" not in render("v2", "bash")


def test_test_set_shape():
    assert len(TEST_CASES) >= 15
    assert len({c.id for c in TEST_CASES}) == len(TEST_CASES)
    for shell in SHELLS:
        cases_for(shell)  # raises if a COMMAND case lacks a pattern for this shell


def test_test_set_is_weighted_toward_failure_modes():
    happy = {"basic", "shell_awareness"}
    adversarial = [c for c in TEST_CASES if c.category not in happy]
    assert len(adversarial) > len(TEST_CASES) / 2
