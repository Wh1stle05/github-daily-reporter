"""Run one bounded Hermes editorial process in an independent process group."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import shutil

from github_daily_reporter.editorial import prepare_attempt


TERMINATION_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class HermesEditorialResult:
    returncode: int
    stdout: str
    stderr: str
    attempt_id: str


async def run_hermes_editorial(
    run_dir: Path,
    attempt_id: str,
    *,
    timeout_seconds: float = 900,
    hermes_executable: str | None = None,
) -> HermesEditorialResult:
    """Start Hermes once, stop its whole process group on timeout, and return output."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    attempt_dir = prepare_attempt(run_dir, attempt_id)
    executable = hermes_executable or shutil.which("hermes") or "hermes"
    prompt = _prompt(run_dir, attempt_dir)
    project_root = run_dir.resolve().parents[2]
    process = await asyncio.create_subprocess_exec(
        executable,
        "-z",
        prompt,
        "--skills",
        "github-daily-reporter",
        cwd=project_root,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except asyncio.TimeoutError as error:
        await _terminate_process_group(process)
        raise TimeoutError("hermes editorial timed out") from error
    clean_stdout = _sanitize(stdout.decode("utf-8", errors="replace"))
    clean_stderr = _sanitize(stderr.decode("utf-8", errors="replace"))
    if process.returncode != 0:
        raise RuntimeError(f"hermes editorial failed ({process.returncode})")
    return HermesEditorialResult(process.returncode, clean_stdout, clean_stderr, attempt_id)


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), TERMINATION_GRACE_SECONDS)
        return
    except (asyncio.TimeoutError, AttributeError):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        await process.wait()
    except AttributeError:
        pass


def _prompt(run_dir: Path, attempt_dir: Path) -> str:
    return (
        "Use the github-daily-reporter skill. Read the editorial handoff at "
        f"{run_dir / 'editorial-input.json'}. Write only "
        f"{attempt_dir / 'growth-report.md'} and {attempt_dir / 'mature-report.md'}; "
        "then stop. Do not send Telegram messages or run collection/rank scripts."
    )


def _sanitize(value: str) -> str:
    return "".join(character if character.isprintable() or character in "\n\t" else " " for character in value)
