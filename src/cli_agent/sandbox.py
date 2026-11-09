"""Bonus: verify a generated command by actually running it - in a container.

Why a container and not `subprocess.run`
----------------------------------------
The whole point of the exercise is that we do not trust the command. Running it
on the host would let a single bad generation delete files or change settings.
The container is disposable, has no network, a read-only root filesystem, no
capabilities, a memory/CPU/pid ceiling, and runs as `nobody`. Worst case, it
dies and takes nothing with it.

Why bash and not cmd
--------------------
The commands in the assignment (`ipconfig`, `dir /o-s`, `tasklist`) are Windows
commands, and running Windows commands in a container needs a Windows container
image and a Windows-container-mode Docker daemon. Rather than pretend, the
sandbox targets the `bash` shell: pick "Linux bash" in the app and the same
instruction is translated to `ip addr show`, `ls -lhS`, `ps aux` - which the
sandbox really can execute and verify. `sandbox_capable` on the Shell dataclass
is what encodes this.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass

from .config import PROJECT_ROOT, SHELLS, settings
from .safety import SafetyLevel, classify


@dataclass
class SandboxResult:
    ran: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.ran and self.exit_code == 0

    @property
    def summary(self) -> str:
        if not self.ran:
            return f"skipped: {self.skipped_reason}"
        verdict = "exit 0" if self.exit_code == 0 else f"exit {self.exit_code}"
        return f"{verdict} in {self.duration_ms} ms"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def image_exists(image: str | None = None) -> bool:
    image = image or settings.sandbox_image
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def build_image(image: str | None = None) -> tuple[bool, str]:
    """Build the sandbox image from docker/Dockerfile.sandbox."""
    image = image or settings.sandbox_image
    dockerfile = PROJECT_ROOT / "docker" / "Dockerfile.sandbox"
    if not dockerfile.exists():
        return False, f"missing {dockerfile}"
    proc = subprocess.run(
        ["docker", "build", "-t", image, "-f", str(dockerfile), str(dockerfile.parent)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]


def _docker_args(container_name: str) -> list[str]:
    args = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=32m",
        "--tmpfs", "/work:rw,nosuid,size=32m",
        "--memory", settings.sandbox_memory,
        "--cpus", settings.sandbox_cpus,
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--workdir", "/work",
    ]
    if not settings.sandbox_allow_network:
        args += ["--network", "none"]
    return args


def _kill(container_name: str) -> None:
    subprocess.run(
        ["docker", "kill", container_name],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def run_in_sandbox(
    command: str,
    shell_key: str = "bash",
    confirmed: bool = False,
) -> SandboxResult:
    """Run `command` inside a disposable container.

    Refuses to run anything the guard calls BLOCKED, and anything it calls RISKY
    unless `confirmed=True`. This mirrors what the UI enforces, so the harness
    cannot execute something the UI would have stopped.
    """
    command = (command or "").strip()
    if not command:
        return SandboxResult(False, skipped_reason="empty command")

    shell = SHELLS.get(shell_key)
    if shell is None or not shell.sandbox_capable:
        return SandboxResult(
            False,
            skipped_reason=f"the sandbox image is Linux; {shell_key} commands cannot run in it",
        )

    if not settings.sandbox_enabled:
        return SandboxResult(False, skipped_reason="SANDBOX_ENABLED=false")

    verdict = classify(command, posix=shell.posix)
    if verdict.level is SafetyLevel.BLOCKED:
        return SandboxResult(False, skipped_reason=f"blocked by guard: {verdict.reason}")
    if verdict.level is SafetyLevel.RISKY and not confirmed:
        return SandboxResult(False, skipped_reason=f"needs confirmation: {verdict.reason}")

    if not docker_available():
        return SandboxResult(False, skipped_reason="docker is not installed or the daemon is down")
    if not image_exists():
        return SandboxResult(
            False,
            skipped_reason=f"image {settings.sandbox_image} not built - run `python -m cli_agent.sandbox build`",
        )

    container_name = f"cli-agent-{uuid.uuid4().hex[:12]}"
    # The image's ENTRYPOINT seeds /work and hands the whole line to `bash -c`.
    argv = _docker_args(container_name) + [settings.sandbox_image, command]

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=settings.sandbox_timeout_sec,
        )
    except subprocess.TimeoutExpired:
        _kill(container_name)
        return SandboxResult(
            True,
            exit_code=124,
            stderr=f"timed out after {settings.sandbox_timeout_sec}s",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except OSError as exc:
        return SandboxResult(False, skipped_reason=f"failed to start docker: {exc}")

    return SandboxResult(
        ran=True,
        exit_code=proc.returncode,
        stdout=proc.stdout[-4000:],
        stderr=proc.stderr[-4000:],
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _cli() -> int:  # pragma: no cover
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "build":
        ok, log = build_image()
        print(log)
        print("built" if ok else "build failed")
        return 0 if ok else 1

    if len(sys.argv) > 1 and sys.argv[1] == "check":
        print(f"docker available: {docker_available()}")
        print(f"image {settings.sandbox_image} present: {image_exists()}")
        return 0

    command = " ".join(sys.argv[1:]) or "ls -lhS"
    result = run_in_sandbox(command, confirmed=False)
    print(result.summary)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
