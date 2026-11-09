"""Deterministic safety guard.

This is the *code* layer of a two-layer defence:

1. The prompt tells the model to refuse destructive requests (see prompts/v3, v4).
2. This module re-classifies whatever the model produced, no matter what it said.

Layer 2 never trusts layer 1. A model that is talked into emitting `format c:`
still gets stopped here, because `classify()` looks at the command text alone.

Levels
------
SAFE    - read-only or clearly reversible. May run.
RISKY   - mutates state in a bounded way. Requires explicit user confirmation.
BLOCKED - unbounded destruction, system control, or privilege escalation.
          Never returned to the user as runnable, never executed.

Note on `del` / `rm`
--------------------
The assignment lists `del` among the "forbidden" commands, yet its own worked
example maps "delete the .tmp files in downloads" to `del downloads\\*.tmp`.
Reading `del` as always-forbidden makes that example unanswerable. The policy
implemented here resolves the tension by scope rather than by verb:

    del downloads\\*.tmp     -> RISKY   (bounded, needs confirmation)
    del /s /q C:\\*          -> BLOCKED (unbounded, system scope)

Set SAFETY_STRICT_DELETE=true in .env to get the literal reading instead, where
any delete verb is BLOCKED. See docs/ITERATIONS.md for the full argument.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum

from .config import settings


class SafetyLevel(str, Enum):
    SAFE = "SAFE"
    RISKY = "RISKY"
    BLOCKED = "BLOCKED"

    @property
    def rank(self) -> int:
        return {"SAFE": 0, "RISKY": 1, "BLOCKED": 2}[self.value]


@dataclass(frozen=True)
class SafetyVerdict:
    level: SafetyLevel
    reason: str
    rule: str

    @property
    def requires_confirmation(self) -> bool:
        return self.level is SafetyLevel.RISKY

    @property
    def may_execute(self) -> bool:
        return self.level is not SafetyLevel.BLOCKED


@dataclass(frozen=True)
class _Rule:
    level: SafetyLevel
    name: str
    pattern: re.Pattern[str]
    reason: str


def _r(level: SafetyLevel, name: str, pattern: str, reason: str) -> _Rule:
    return _Rule(level, name, re.compile(pattern, re.IGNORECASE), reason)


# --------------------------------------------------------------------------
# Rules matched against a whole command segment.
# BLOCKED rules are evaluated before RISKY rules.
# --------------------------------------------------------------------------
BLOCKED_RULES: tuple[_Rule, ...] = (
    _r(SafetyLevel.BLOCKED, "power_control",
       r"\b(shutdown|reboot|poweroff|halt)\b|\binit\s+0\b|\b(Stop|Restart)-Computer\b",
       "Shuts down or restarts the machine."),
    _r(SafetyLevel.BLOCKED, "disk_format",
       r"\bformat\s+[a-z]:|\bFormat-Volume\b|\bmkfs(\.\w+)?\b|\bdiskpart\b",
       "Formats or repartitions a disk."),
    _r(SafetyLevel.BLOCKED, "raw_disk_write",
       r"\bdd\b[^|;&]*\bof=\s*/dev/|>\s*/dev/(sd|nvme|hd)",
       "Writes directly to a block device."),
    _r(SafetyLevel.BLOCKED, "fork_bomb",
       r":\s*\(\s*\)\s*\{.*\}\s*;\s*:",
       "Fork bomb - exhausts process table."),
    _r(SafetyLevel.BLOCKED, "boot_or_shadow_copies",
       r"\bbcdedit\b|\bvssadmin\b[^|;&]*\bdelete\b|\bcipher\b\s+/w|\bwbadmin\b[^|;&]*\bdelete\b",
       "Destroys boot configuration or system backups."),
    _r(SafetyLevel.BLOCKED, "registry_delete",
       r"\breg\s+delete\b|\bRemove-Item\b[^|;&]*\bHK(LM|CU|CR|U|CC):",
       "Deletes registry keys."),
    _r(SafetyLevel.BLOCKED, "pipe_to_shell",
       r"\b(curl|wget|iwr|Invoke-WebRequest)\b[^|]*\|\s*(sudo\s+)?(ba|z|s)?sh\b|\biex\b\s*\(",
       "Downloads and executes remote code."),
    _r(SafetyLevel.BLOCKED, "world_writable_root",
       r"\bchmod\b\s+(-R\s+)?0?777\s+/\s*$|\bchown\b\s+-R\b[^|;&]*\s+/\s*$",
       "Destroys permissions on the filesystem root."),
    _r(SafetyLevel.BLOCKED, "seize_system_drive",
       r"\btakeown\b[^|;&]*/f\s+[a-z]:\\?\s*$|\bicacls\b\s+[a-z]:\\?\s+/grant",
       "Seizes ownership of the system drive."),
    _r(SafetyLevel.BLOCKED, "history_or_journal_wipe",
       r"\bshred\b|\bsrm\b|\bwevtutil\b\s+cl\b",
       "Irreversibly erases data or audit logs."),
)

RISKY_RULES: tuple[_Rule, ...] = (
    _r(SafetyLevel.RISKY, "process_kill",
       r"\b(kill|pkill|killall|taskkill)\b|\bStop-Process\b",
       "Terminates running processes."),
    _r(SafetyLevel.RISKY, "move_or_rename",
       r"\b(mv|move|ren|rename)\b|\b(Move|Rename)-Item\b",
       "Moves or renames files."),
    _r(SafetyLevel.RISKY, "permissions",
       r"\b(chmod|chown|attrib|icacls|Set-Acl)\b",
       "Changes file permissions or ownership."),
    _r(SafetyLevel.RISKY, "privilege_escalation",
       r"\b(sudo|runas|Start-Process\b[^|;&]*-Verb\s+RunAs)\b",
       "Runs with elevated privileges."),
    _r(SafetyLevel.RISKY, "package_management",
       r"\b(apt|apt-get|yum|dnf|apk|brew|choco|winget)\s+(install|remove|purge|uninstall)\b"
       r"|\bpip\s+(install|uninstall)\b|\bnpm\s+(install|uninstall|i)\b",
       "Installs or removes software."),
    _r(SafetyLevel.RISKY, "destructive_git",
       r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+(--force|-f)\b|checkout\s+--\s)",
       "Discards uncommitted work or rewrites remote history."),
    _r(SafetyLevel.RISKY, "service_or_registry_write",
       r"\bsc\s+(stop|start|delete|config)\b|\bnet\s+(user|localgroup|stop|start)\b"
       r"|\bsystemctl\s+(stop|start|restart|disable|enable)\b|\breg\s+add\b",
       "Changes services, users, or registry values."),
    _r(SafetyLevel.RISKY, "container_pruning",
       r"\bdocker\s+(rm|rmi|volume\s+rm|system\s+prune)\b",
       "Removes containers, images, or volumes."),
    _r(SafetyLevel.RISKY, "truncating_redirect",
       r"(?<![>\d])>(?!>)\s*\S+",
       "Overwrites the target file via `>` redirection."),
    _r(SafetyLevel.RISKY, "firewall_or_network_config",
       r"\b(iptables|ufw|netsh)\b",
       "Changes network or firewall configuration."),
)

# Delete verbs are handled by _classify_delete(), not by a flat regex, because
# their level depends on the target's scope.
DELETE_VERBS = {"rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "unlink"}
_HELP_FLAGS = {"/?", "-h", "-?", "--help", "-help"}

_DANGEROUS_TARGETS = {
    "/", "/*", "/.", "~", "~/", "~/*", "*", "*.*", ".", "./", "./*", "..", "../*",
    "$home", "$home/*", "$env:userprofile", "$env:systemdrive", "$env:windir",
    "%systemdrive%", "%windir%", "%userprofile%", "%homepath%",
    "/home", "/home/*", "/etc", "/usr", "/var", "/boot", "/bin", "/sbin", "/lib", "/root",
    "c:", "c:\\", "c:\\*", "c:/", "c:/*", "c:\\windows", "c:\\users", "c:\\program files",
}

_DRIVE_ROOT = re.compile(r"^[a-z]:[\\/]?\*?$", re.IGNORECASE)


def _normalize_target(token: str) -> str:
    token = token.strip().strip('"').strip("'").lower()
    if len(token) > 1 and token.endswith(("/", "\\")):
        token = token[:-1] or token
    return token


def _is_dangerous_target(token: str) -> bool:
    norm = _normalize_target(token)
    return norm in _DANGEROUS_TARGETS or bool(_DRIVE_ROOT.match(norm))


def _tokenize(segment: str, posix: bool) -> list[str]:
    try:
        return shlex.split(segment, posix=posix)
    except ValueError:
        return segment.split()


def _classify_delete(tokens: list[str], posix: bool) -> SafetyVerdict | None:
    """Return a verdict if `tokens` is a delete command, else None."""
    if not tokens:
        return None
    verb = tokens[0].lower().lstrip("&")
    if verb not in DELETE_VERBS:
        return None

    if settings.safety_strict_delete:
        return SafetyVerdict(
            SafetyLevel.BLOCKED,
            f"`{verb}` is on the forbidden list (SAFETY_STRICT_DELETE is on).",
            "delete_strict",
        )

    args = tokens[1:]
    flags = [a for a in args if _is_flag(a, posix)]
    targets = [a for a in args if not _is_flag(a, posix)]

    # `del /?` and `rm --help` print a help page and delete nothing. Classifying
    # them as RISKY teaches the user to click through confirmations, which is how
    # a confirmation prompt stops meaning anything.
    if flags and all(f.lower() in _HELP_FLAGS for f in flags) and not targets:
        return SafetyVerdict(SafetyLevel.SAFE, f"`{verb}` help page; deletes nothing.", "delete_help")

    flag_text = " ".join(f.lower() for f in flags)
    recursive = bool(re.search(r"(^|\s)-{1,2}[a-z]*r|/s\b|--recursive|-recurse", flag_text))
    forced = bool(re.search(r"(^|\s)-{1,2}[a-z]*f|/q\b|/f\b|--force|-force", flag_text))

    if any(_is_dangerous_target(t) for t in targets):
        return SafetyVerdict(
            SafetyLevel.BLOCKED,
            "Deletes a system-scope or unbounded target (filesystem root, home, or drive root).",
            "delete_unbounded_target",
        )
    if recursive and forced and not targets:
        return SafetyVerdict(
            SafetyLevel.BLOCKED,
            "Recursive forced delete with no explicit target.",
            "delete_no_target",
        )
    if recursive and forced:
        return SafetyVerdict(
            SafetyLevel.BLOCKED,
            "Recursive + forced delete. Refuse and ask the user to narrow the scope.",
            "delete_recursive_forced",
        )
    return SafetyVerdict(
        SafetyLevel.RISKY,
        f"`{verb}` removes files. Bounded in scope, but needs explicit user confirmation.",
        "delete_scoped",
    )


def _is_flag(token: str, posix: bool) -> bool:
    """`/s` is a flag on Windows but `/etc` is a path on POSIX."""
    if token in {"-", "--"}:
        return False
    if token.startswith("-"):
        return True
    return not posix and token.startswith("/")


_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;|&(?!\d)")


def split_segments(command: str) -> list[str]:
    """Split a chained command into individually-classifiable segments."""
    return [seg.strip() for seg in _SEGMENT_SPLIT.split(command) if seg.strip()]


def classify(command: str, posix: bool = False) -> SafetyVerdict:
    """Classify a command. The strictest segment wins."""
    command = (command or "").strip()
    if not command:
        return SafetyVerdict(SafetyLevel.SAFE, "Empty command.", "empty")

    verdicts: list[SafetyVerdict] = []

    # Some patterns (pipe-to-shell, fork bombs) only exist across segments.
    for rule in BLOCKED_RULES:
        if rule.pattern.search(command):
            verdicts.append(SafetyVerdict(rule.level, rule.reason, rule.name))

    for segment in split_segments(command):
        for rule in BLOCKED_RULES:
            if rule.pattern.search(segment):
                verdicts.append(SafetyVerdict(rule.level, rule.reason, rule.name))

        delete_verdict = _classify_delete(_tokenize(segment, posix), posix)
        if delete_verdict is not None:
            verdicts.append(delete_verdict)

        for rule in RISKY_RULES:
            if rule.pattern.search(segment):
                verdicts.append(SafetyVerdict(rule.level, rule.reason, rule.name))

    if not verdicts:
        return SafetyVerdict(SafetyLevel.SAFE, "Read-only or reversible.", "default_safe")

    return max(verdicts, key=lambda v: v.level.rank)


# --------------------------------------------------------------------------
# Single source of truth for the policy text embedded in prompts v3 and v4.
# Editing the rules above without editing the prompt is how the two layers
# drift apart, so the prompt imports this string instead of restating it.
# --------------------------------------------------------------------------
POLICY_SUMMARY = """\
NEVER emit a command that does any of the following. Answer `BLOCKED: <reason>` instead:
  - shuts down, reboots or halts the machine (shutdown, reboot, Stop-Computer)
  - formats, repartitions or writes raw blocks to a disk (format, mkfs, diskpart, dd of=/dev/...)
  - deletes an unbounded or system-scope target: the filesystem root (/), a drive
    root (C:\\), the home directory, or a recursive+forced delete (rm -rf, del /s /q, rd /s)
  - deletes registry keys, boot config, shadow copies or event logs
  - downloads and pipes remote code into a shell (curl ... | sh)
  - grants world access to the system drive (chmod 777 /, icacls C:\\ /grant)

These are allowed but are RISKY - emit the command normally; the host application
will ask the user to confirm before running it:
  - a scoped delete (del downloads\\*.tmp, rm ./build/*.o)
  - killing a process, moving/renaming files, changing permissions
  - installing or removing packages, `git reset --hard`
  - overwriting a file with `>`"""
