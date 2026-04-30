from __future__ import annotations

import asyncio
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

import database as db

log = logging.getLogger("deploy-hook")

GIT_TIMEOUT_SECONDS = 300
STEP_TIMEOUT_SECONDS = 1800

_DEPLOY_LOCKS: dict[int, asyncio.Lock] = {}


def _project_lock(project_id: int) -> asyncio.Lock:
    lock = _DEPLOY_LOCKS.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _DEPLOY_LOCKS[project_id] = lock
    return lock


@dataclass(slots=True)
class CommandResult:
    display: str
    cwd: Path
    duration_seconds: float
    output: str
    returncode: int | None
    timed_out: bool
    timeout_seconds: int

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.returncode == 0


def _render_section(title: str, body: str) -> str:
    return f"=== {title} ===\n{body.strip()}"


def _render_command(result: CommandResult) -> str:
    lines = [
        f"$ (cd {shlex.quote(str(result.cwd))} && {result.display})",
        f"duration: {result.duration_seconds:.1f}s",
    ]
    if result.timed_out:
        lines.append(f"status: timed out after {result.timeout_seconds}s")
    else:
        lines.append(f"exit_code: {result.returncode}")
    lines.append("")
    lines.append(result.output.strip() or "(no output)")
    return "\n".join(lines)


def _is_git_repo(deploy_path: Path) -> bool:
    return (deploy_path / ".git").exists()


def _is_directory_empty(path: Path) -> bool:
    return not any(path.iterdir())


async def _run_argv(argv: tuple[str, ...], cwd: Path, timeout_seconds: int) -> CommandResult:
    started_at = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    return await _consume(proc, shlex.join(argv), cwd, started_at, timeout_seconds)


async def _run_shell(command: str, cwd: Path, timeout_seconds: int) -> CommandResult:
    started_at = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    return await _consume(proc, command, cwd, started_at, timeout_seconds)


async def _consume(
    proc: asyncio.subprocess.Process,
    display: str,
    cwd: Path,
    started_at: float,
    timeout_seconds: int,
) -> CommandResult:
    timed_out = False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace") if stdout else ""
    return CommandResult(
        display=display,
        cwd=cwd,
        duration_seconds=time.monotonic() - started_at,
        output=output,
        returncode=None if timed_out else proc.returncode,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
    )


async def _ensure_source(project: dict, deploy_path: Path) -> tuple[CommandResult, str]:
    """Clone or pull the repo into deploy_path. Returns (result, section title)."""
    branch = project["branch"]
    if not deploy_path.exists():
        deploy_path.parent.mkdir(parents=True, exist_ok=True)
        result = await _run_argv(
            (
                "git", "clone",
                "--branch", branch,
                "--single-branch",
                project["repo_url"],
                str(deploy_path),
            ),
            cwd=deploy_path.parent,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
        )
        return result, "git clone"
    if not deploy_path.is_dir():
        raise FileNotFoundError(f"Deploy path is not a directory: {deploy_path}")
    if _is_git_repo(deploy_path):
        result = await _run_argv(
            (
                "git",
                "-c", f"safe.directory={deploy_path}",
                "pull", "origin", branch,
            ),
            cwd=deploy_path,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
        )
        return result, "git pull"
    if _is_directory_empty(deploy_path):
        result = await _run_argv(
            (
                "git", "clone",
                "--branch", branch,
                "--single-branch",
                project["repo_url"],
                ".",
            ),
            cwd=deploy_path,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
        )
        return result, "git clone"
    raise FileNotFoundError(f"Deploy path exists but is not a git repository: {deploy_path}")


async def _run_step(step: dict, deploy_path: Path) -> CommandResult:
    command = step["command"]
    if step["use_shell"]:
        return await _run_shell(command, cwd=deploy_path, timeout_seconds=STEP_TIMEOUT_SECONDS)
    argv = tuple(shlex.split(command))
    if not argv:
        raise ValueError(f"Step '{step['name']}' has empty command.")
    return await _run_argv(argv, cwd=deploy_path, timeout_seconds=STEP_TIMEOUT_SECONDS)


async def run_deploy(project: dict, commit_sha: str | None = None, commit_message: str | None = None) -> None:
    log_id = await db.create_deploy_log(project["id"], commit_sha, commit_message)
    log.info("Deploy queued: project=%s log_id=%d", project["name"], log_id)

    async with _project_lock(project["id"]):
        log.info("Deploy started: project=%s log_id=%d", project["name"], log_id)
        deploy_path = Path(project["deploy_path"]).expanduser()

        sections = [
            _render_section(
                "Deploy Context",
                "\n".join([
                    f"project: {project['name']}",
                    f"deploy_path: {deploy_path}",
                    f"branch: {project['branch']}",
                    f"commit_sha: {commit_sha or '-'}",
                    f"commit_message: {commit_message or 'Manual deploy'}",
                ]),
            )
        ]
        status = "success"

        try:
            git_result, git_title = await _ensure_source(project, deploy_path)
            sections.append(_render_section(git_title, _render_command(git_result)))
            if not git_result.succeeded:
                status = "failed"
            else:
                steps = await db.get_enabled_steps(project["id"])
                if not steps:
                    sections.append(_render_section(
                        "Pipeline",
                        "활성화된 단계가 없습니다. 프로젝트 설정에서 단계를 추가하세요.",
                    ))
                    status = "failed"
                else:
                    for index, step in enumerate(steps, start=1):
                        title = f"step {index}: {step['name']}"
                        try:
                            result = await _run_step(step, deploy_path)
                        except Exception as exc:
                            sections.append(_render_section(title, f"error: {exc}"))
                            status = "failed"
                            break
                        sections.append(_render_section(title, _render_command(result)))
                        if not result.succeeded:
                            status = "failed"
                            break
        except Exception as exc:
            sections.append(_render_section("error", str(exc)))
            status = "failed"

        output = "\n\n".join(sections)
        await db.finish_deploy_log(log_id, status, output)
        log.info("Deploy finished: project=%s status=%s", project["name"], status)
