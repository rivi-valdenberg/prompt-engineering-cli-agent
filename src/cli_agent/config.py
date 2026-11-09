"""Central configuration: environment, target shells, paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
DOCS_DIR = PROJECT_ROOT / "docs"

load_dotenv(PROJECT_ROOT / ".env")


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env_str(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Shell:
    """A target shell the agent can generate commands for.

    Two of them: `cmd`, because that is what the assignment's examples are written
    in, and `bash`, because the Docker sandbox is a Linux container and only bash
    commands can actually be executed and verified in it.
    """

    key: str
    display: str
    os_name: str
    shell_name: str
    posix: bool  # how to tokenize: POSIX quoting rules vs Windows
    sandbox_capable: bool
    hints: str


CMD_HINTS = """\
- Network info: `ipconfig`, `ipconfig /all`
- List files: `dir`; sorted by size descending: `dir /o-s`; recursive: `dir /s`
- Processes: `tasklist`; kill by name: `taskkill /IM <name>`
- Files: `del <pattern>`, `copy`, `move`, `ren`, `mkdir`, `type <file>`
- Search inside files: `findstr <pattern> <file>`
- Disk space: `wmic logicaldisk get size,freespace,caption`
- Environment variables use the `%VAR%` syntax
- Chain with `&` (always run next) or `&&` (only if previous succeeded)
- cmd has no native `tail`/`head`; use `more` or delegate: powershell -Command "..."
- Paths with spaces must be wrapped in double quotes"""

BASH_HINTS = """\
- Network info: `ip addr show` (or `hostname -I`)
- List files sorted by size descending: `ls -lhS`; recursive: `ls -R`
- Processes: `ps aux`; kill: `kill <pid>` / `pkill <name>`
- Files: `rm`, `cp`, `mv`, `mkdir -p`, `cat <file>`
- Last lines of a file: `tail -n 20 <file>`
- Search inside files: `grep <pattern> <file>`
- Disk space: `df -h`
- Environment variables use the `$VAR` syntax
- Chain with `&&` (only if previous succeeded) or `;` (always)
- Paths with spaces must be quoted or escaped"""


SHELLS: dict[str, Shell] = {
    "cmd": Shell(
        key="cmd",
        display="Windows CMD",
        os_name="Windows 11",
        shell_name="Windows Command Prompt (cmd.exe)",
        posix=False,
        sandbox_capable=False,
        hints=CMD_HINTS,
    ),
    "bash": Shell(
        key="bash",
        display="Linux bash",
        os_name="Ubuntu Linux",
        shell_name="GNU bash",
        posix=True,
        sandbox_capable=True,
        hints=BASH_HINTS,
    ),
}


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    gemini_temperature: float
    gemini_thinking_budget: int
    gemini_max_output_tokens: int
    gemini_rpm: int
    default_shell: str
    default_prompt_version: str
    safety_strict_delete: bool
    sandbox_enabled: bool
    sandbox_image: str
    sandbox_timeout_sec: int
    sandbox_memory: str
    sandbox_cpus: str
    sandbox_allow_network: bool
    eval_workers: int


def load_settings() -> Settings:
    shell = _env_str("DEFAULT_SHELL", "cmd").lower()
    if shell not in SHELLS:
        shell = "cmd"
    return Settings(
        gemini_api_key=_env_str("GEMINI_API_KEY", ""),
        gemini_model=_env_str("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        gemini_temperature=_env_float("GEMINI_TEMPERATURE", 0.0),
        # -1 = send no thinking config at all. gemini-2.5-flash reasons by default
        # and spends ~55 output tokens doing it, which is harmless under a 512-token
        # ceiling. Newer models reject the field outright, so `llm.py` also drops it
        # on the first 400 that mentions it.
        gemini_thinking_budget=_env_int("GEMINI_THINKING_BUDGET", -1),
        gemini_max_output_tokens=_env_int("GEMINI_MAX_OUTPUT_TOKENS", 512),
        gemini_rpm=_env_int("GEMINI_RPM", 10),
        default_shell=shell,
        default_prompt_version=_env_str("DEFAULT_PROMPT_VERSION", "v3"),
        safety_strict_delete=_env_bool("SAFETY_STRICT_DELETE", False),
        sandbox_enabled=_env_bool("SANDBOX_ENABLED", True),
        sandbox_image=_env_str("SANDBOX_IMAGE", "cli-agent-sandbox:latest"),
        sandbox_timeout_sec=_env_int("SANDBOX_TIMEOUT_SEC", 10),
        sandbox_memory=_env_str("SANDBOX_MEMORY", "256m"),
        sandbox_cpus=_env_str("SANDBOX_CPUS", "0.5"),
        sandbox_allow_network=_env_bool("SANDBOX_ALLOW_NETWORK", False),
        eval_workers=_env_int("EVAL_WORKERS", 4),
    )


settings = load_settings()
