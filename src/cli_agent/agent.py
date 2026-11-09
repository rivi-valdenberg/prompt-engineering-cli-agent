"""The agent. Deliberately thin - the work lives in the prompt.

`generate()` does four things:
  1. renders the chosen prompt iteration for the chosen shell
  2. calls Gemini
  3. parses the reply into a decision (COMMAND / BLOCKED / UNSUPPORTED / CLARIFY)
  4. runs the deterministic guard over whatever came back

Step 4 can override step 3. A model that answers with `format c:` gets its
decision rewritten to BLOCKED here, and `guard_overrode_model` is set - that flag
is what tells you whether the prompt is carrying its own weight or whether the
code is quietly covering for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .config import SHELLS, Shell, settings
from .llm import GeminiLLM, LLMResponse
from .prompts import LATEST, render
from .safety import SafetyLevel, SafetyVerdict, classify
from .validator import check_format, check_syntax, looks_wrong_shell


class Decision(str, Enum):
    COMMAND = "COMMAND"
    BLOCKED = "BLOCKED"
    UNSUPPORTED = "UNSUPPORTED"
    CLARIFY = "CLARIFY"
    # The model wrote prose instead of any of the four allowed forms. Not a
    # command, not a refusal we can act on. Crucially, NOT an unsafe leak either:
    # a paragraph of Hebrew explaining why it will not wipe your disk is a format
    # failure, not a dangerous one, and nothing downstream can execute it.
    MALFORMED = "MALFORMED"


class SupportsComplete(Protocol):
    def complete(self, system_prompt: str, user_text: str) -> LLMResponse: ...


@dataclass
class AgentResult:
    instruction: str
    prompt_version: str
    shell_key: str
    raw_output: str
    decision: Decision
    # What the model itself decided, before the deterministic guard had a say.
    # `decision` may differ; the gap between them is the prompt's blind spot.
    model_decision: Decision = Decision.COMMAND
    command: str = ""
    reason: str = ""
    safety: SafetyVerdict = field(
        default_factory=lambda: SafetyVerdict(SafetyLevel.SAFE, "", "n/a")
    )
    format_issues: list[str] = field(default_factory=list)
    syntax_issues: list[str] = field(default_factory=list)
    shell_mismatch: str | None = None
    guard_overrode_model: bool = False
    latency_ms: int = 0
    model: str = ""
    error: str = ""

    @property
    def format_ok(self) -> bool:
        return not self.format_issues

    @property
    def syntax_ok(self) -> bool:
        # syntax_issues is only populated when the model tried to produce a
        # command, so a clean BLOCKED/UNSUPPORTED/CLARIFY reply passes for free,
        # while a guard-overridden dangerous command keeps the issues it earned.
        return not self.syntax_issues and self.shell_mismatch is None

    @property
    def requires_confirmation(self) -> bool:
        return self.decision is Decision.COMMAND and self.safety.requires_confirmation

    @property
    def may_execute(self) -> bool:
        return self.decision is Decision.COMMAND and self.safety.may_execute

    @property
    def display_text(self) -> str:
        if self.decision is Decision.COMMAND:
            return self.command
        if self.decision is Decision.MALFORMED:
            return self.raw_output.strip()
        return f"{self.decision.value}: {self.reason}"


_CONTROL = re.compile(
    r"^(?P<kind>BLOCKED|UNSUPPORTED|CLARIFY)\s*[:\-]\s*(?P<reason>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_FENCE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\s*(.*?)\s*```", re.DOTALL)


def _salvage_command(raw: str) -> str:
    """Extract the command a sloppy answer was trying to give.

    Kept separate from the format check on purpose: an answer can be badly
    formatted *and* contain the right command, and we want to score those two
    facts independently.
    """
    text = raw.strip()

    fenced = _FENCE_BLOCK.search(text)
    if fenced:
        text = fenced.group(1).strip()

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    line = lines[0]

    line = re.sub(r"^(\$|#|>|C:\\[^>]*>|PS\s+[A-Z]:\\[^>]*>)\s+", "", line)
    line = line.strip("`").strip()
    if len(line) > 1 and line[0] == line[-1] and line[0] in {'"', "'"}:
        line = line[1:-1].strip()
    return line


_NOT_A_COMMAND = {"implausible_command_word", "looks_like_a_sentence", "empty_command"}


def is_executable_shape(command: str, syntax_issues: list[str]) -> bool:
    """Could a shell plausibly run this string, or is it a sentence?

    This is the difference between "the model produced a dangerous command" and
    "the model produced a paragraph that happens to mention deleting things".
    Only the first one is a safety event.
    """
    if not command.strip():
        return False
    return not any(issue.split("(")[0] in _NOT_A_COMMAND for issue in syntax_issues)


def parse_output(raw: str) -> tuple[Decision, str, str]:
    """-> (decision, command, reason)"""
    text = (raw or "").strip()
    if not text:
        return Decision.UNSUPPORTED, "", "the model returned an empty answer"

    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    control = _CONTROL.match(first_line)
    if control:
        kind = control.group("kind").upper()
        reason = control.group("reason").strip() or "(no reason given)"
        return Decision(kind), "", reason

    return Decision.COMMAND, _salvage_command(text), ""


def evaluate_output(
    instruction: str,
    raw: str,
    prompt_version: str,
    shell_key: str,
    model: str = "",
    latency_ms: int = 0,
) -> AgentResult:
    """Everything that happens *after* the model has spoken. Pure.

    The model call is the only impure, expensive, non-deterministic step in this
    system. Keeping the rest of the pipeline a pure function of `raw` means a
    finished experiment can be re-scored offline when a metric turns out to be
    wrong - without spending a token, and without the temptation to hand-edit a
    results file. See `cli-agent-eval --rescore`.
    """
    shell: Shell = SHELLS[shell_key]
    decision, command, reason = parse_output(raw)

    result = AgentResult(
        instruction=instruction,
        prompt_version=prompt_version,
        shell_key=shell_key,
        raw_output=raw,
        decision=decision,
        model_decision=decision,
        command=command,
        reason=reason,
        latency_ms=latency_ms,
        model=model,
    )
    result.format_issues = check_format(raw)

    if decision is Decision.COMMAND:
        result.syntax_issues = check_syntax(command, posix=shell.posix)
        result.shell_mismatch = looks_wrong_shell(command, shell_key)

        if not is_executable_shape(command, result.syntax_issues):
            # Prose, not a command. Never hand it to a shell, and never count it
            # as a dangerous leak - there is nothing here to execute.
            result.decision = Decision.MALFORMED
            result.model_decision = Decision.MALFORMED
            result.reason = "the model answered in prose instead of a single command"
            result.safety = SafetyVerdict(SafetyLevel.SAFE, "Not an executable command.", "n/a")
            return result

        result.safety = classify(command, posix=shell.posix)
        if result.safety.level is SafetyLevel.BLOCKED:
            # The prompt let it through; the guard did not.
            result.guard_overrode_model = True
            result.decision = Decision.BLOCKED
            result.reason = f"guard: {result.safety.reason}"
    else:
        result.safety = SafetyVerdict(SafetyLevel.SAFE, "No command produced.", "n/a")

    return result


def generate(
    instruction: str,
    prompt_version: str = LATEST,
    shell_key: str | None = None,
    llm: SupportsComplete | None = None,
) -> AgentResult:
    """Ask the model, then evaluate what it said."""
    shell_key = shell_key or settings.default_shell
    system_prompt = render(prompt_version, shell_key)
    client = llm or GeminiLLM()

    response = client.complete(system_prompt, instruction)
    return evaluate_output(
        instruction=instruction,
        raw=response.text,
        prompt_version=prompt_version,
        shell_key=shell_key,
        model=response.model,
        latency_ms=response.latency_ms,
    )
