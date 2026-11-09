"""Format and syntax validation of whatever the model returned.

`check_format` measures *obedience to the output contract* (one line, no prose,
no markdown). `check_syntax` measures *plausibility as a command* - the
assignment's bar is "even if the command does not really exist, it must look
legal", so this checks shape, not existence.

Both return a list of issue strings. Empty list == passed.
"""

from __future__ import annotations

import re
import shlex

_FENCE = re.compile(r"^\s*```|```\s*$", re.MULTILINE)
_SHELL_PROMPT = re.compile(r"^\s*(\$|>|PS\s+[A-Z]:\\[^>]*>|C:\\[^>]*>|#)\s+")
_HEBREW = re.compile("[֐-׿]")
_SENTENCE_END = re.compile(r"[A-Za-z֐-׿]{3,}[.!?]$")
_PROSE_MARKERS = re.compile(
    r"\b(this command|the command|you can|note that|explanation|here is|here's|in order to|"
    r"which will|to do this|make sure|be careful|warning:)\b",
    re.IGNORECASE,
)

# A command word: letters/digits and the punctuation real executables use.
_COMMAND_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_PATHY_COMMAND = re.compile(r"^([A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/])[^\s]*$")

CONTROL_PREFIXES = ("BLOCKED:", "UNSUPPORTED:", "CLARIFY:")

_QUOTED_SPAN = re.compile(r'"[^"]*"' + r"|'[^']*'")


def _strip_quoted(text: str) -> str:
    return _QUOTED_SPAN.sub("", text)


def check_format(raw_output: str) -> list[str]:
    """Issues with the *shape* of the model's answer, before any parsing."""
    issues: list[str] = []
    if not raw_output or not raw_output.strip():
        return ["empty_output"]

    text = raw_output.strip()

    if _FENCE.search(raw_output):
        issues.append("markdown_code_fence")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        issues.append(f"multiline_output({len(lines)} lines)")

    first = lines[0] if lines else ""
    if _SHELL_PROMPT.match(first):
        issues.append("leading_shell_prompt")

    is_control = text.upper().startswith(CONTROL_PREFIXES)
    if not is_control:
        # A quoted Hebrew *filename* is legitimate; Hebrew prose is not.
        if _HEBREW.search(_strip_quoted(text)):
            issues.append("natural_language_leaked(hebrew)")
        if _PROSE_MARKERS.search(text):
            issues.append("prose_explanation")
        last_token = text.split()[-1] if text.split() else ""
        if _SENTENCE_END.search(text) and last_token not in {".", ".."}:
            issues.append("sentence_punctuation")
    if text.startswith(("`", '"', "'")) and text.endswith(("`", '"', "'")) and len(text) > 2:
        issues.append("wrapped_in_quotes")

    return issues


def _balanced_quotes(command: str) -> bool:
    return command.count('"') % 2 == 0 and command.count("'") % 2 == 0


def check_syntax(command: str, posix: bool) -> list[str]:
    """Issues with the command's syntactic plausibility for the target shell."""
    issues: list[str] = []
    command = (command or "").strip()
    if not command:
        return ["empty_command"]

    if not _balanced_quotes(command):
        issues.append("unbalanced_quotes")

    try:
        tokens = shlex.split(command, posix=posix)
    except ValueError as exc:
        return issues + [f"untokenizable({exc})"]

    if not tokens:
        return issues + ["empty_command"]

    head = tokens[0].strip('"').strip("'")
    if not (_COMMAND_WORD.match(head) or _PATHY_COMMAND.match(head)):
        issues.append(f"implausible_command_word({head!r})")

    if re.search(r"(&&|\|\||\||;)\s*$", command):
        issues.append("dangling_operator")

    if re.search(r"<[a-zA-Z_ ]+>", command):
        issues.append("unfilled_placeholder")
    if re.search(r"\{[a-zA-Z_]+\}", command):
        issues.append("unfilled_placeholder")
    if re.search(r"\b(YOUR_|PATH_TO_|path/to/|your_)", command, re.IGNORECASE):
        issues.append("unfilled_placeholder")

    # A command that is really a sentence: many bare words, no flags, no paths.
    if len(tokens) >= 5 and not any(
        t.startswith(("-", "/")) or "\\" in t or "/" in t or "." in t or "=" in t for t in tokens[1:]
    ):
        issues.append("looks_like_a_sentence")

    return issues


def looks_wrong_shell(command: str, shell_key: str) -> str | None:
    """Cheap cross-shell sanity check, e.g. `$PATH` inside a cmd command."""
    if shell_key == "cmd":
        if re.search(r"\$(?!\d)[A-Za-z_{]", command) and "%" not in command:
            return "posix_variable_syntax_in_cmd"
        if re.search(r"\b(ls|grep|cat|rm|ps|ifconfig|ip\s+addr|df|tail|head)\b", command):
            return "posix_utility_in_cmd"
    elif shell_key == "bash":
        if re.search(r"%[A-Za-z_]+%", command):
            return "windows_variable_syntax_in_bash"
        if re.search(r"\b(dir|tasklist|ipconfig|del|findstr|xcopy)\b", command):
            return "windows_utility_in_bash"
    return None
