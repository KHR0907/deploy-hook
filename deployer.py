from __future__ import annotations

import asyncio
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

import broker
import database as db

log = logging.getLogger("deploy-hook")

GIT_TIMEOUT_SECONDS = 300
STEP_TIMEOUT_SECONDS = 1800
READLINE_KEEPALIVE_SECONDS = 30

_DEPLOY_LOCKS: dict[int, asyncio.Lock] = {}


def _project_lock(project_id: int) -> asyncio.Lock:
    lock = _DEPLOY_LOCKS.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _DEPLOY_LOCKS[project_id] = lock
    return lock


@dataclass(slots=True)
class StepResult:
    succeeded: bool


class _Sink:
    """Collects every emitted line for the final DB write and broadcasts it
    live through the broker."""

    def __init__(self, log_id: int) -> None:
        self.log_id = log_id
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        for piece in line.split("\n"):
            self.lines.append(piece)
            broker.publish_line(self.log_id, piece)

    def section(self, title: str) -> None:
        if self.lines:
            self.write("")
        self.write(f"=== {title} ===")

    def text(self) -> str:
        return "\n".join(self.lines)


def _is_git_repo(deploy_path: Path) -> bool:
    return (deploy_path / ".git").exists()


def _is_directory_empty(path: Path) -> bool:
    return not any(path.iterdir())


async def _run(
    *,
    argv: tuple[str, ...] | None,
    shell_cmd: str | None,
    cwd: Path,
    timeout: int,
    sink: _Sink,
) -> StepResult:
    started = time.monotonic()
    if argv is not None:
        display = shlex.join(argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    else:
        assert shell_cmd is not None
        display = shell_cmd
        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    sink.write(f"$ (cd {shlex.quote(str(cwd))} && {display})")

    deadline = started + timeout
    timed_out = False
    assert proc.stdout is not None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            raw = await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=min(remaining, READLINE_KEEPALIVE_SECONDS),
            )
        except asyncio.TimeoutError:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            continue
        if not raw:
            break
        sink.write(raw.decode(errors="replace").rstrip("\r\n"))

    if timed_out:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()
    duration = time.monotonic() - started
    if timed_out:
        sink.write(f"status: timed out after {timeout}s")
        sink.write(f"duration: {duration:.1f}s")
        return StepResult(succeeded=False)
    sink.write(f"duration: {duration:.1f}s")
    sink.write(f"exit_code: {proc.returncode}")
    return StepResult(succeeded=proc.returncode == 0)


async def _ensure_source(project: dict, deploy_path: Path, sink: _Sink) -> StepResult:
    branch = project["branch"]
    if not deploy_path.exists():
        deploy_path.parent.mkdir(parents=True, exist_ok=True)
        sink.section("git clone")
        return await _run(
            argv=(
                "git", "clone",
                "--branch", branch,
                "--single-branch",
                project["repo_url"],
                str(deploy_path),
            ),
            shell_cmd=None,
            cwd=deploy_path.parent,
            timeout=GIT_TIMEOUT_SECONDS,
            sink=sink,
        )
    if not deploy_path.is_dir():
        sink.section("error")
        sink.write(f"Deploy path is not a directory: {deploy_path}")
        return StepResult(succeeded=False)
    if _is_git_repo(deploy_path):
        sink.section("git pull")
        return await _run(
            argv=(
                "git",
                "-c", f"safe.directory={deploy_path}",
                "pull", "origin", branch,
            ),
            shell_cmd=None,
            cwd=deploy_path,
            timeout=GIT_TIMEOUT_SECONDS,
            sink=sink,
        )
    if _is_directory_empty(deploy_path):
        sink.section("git clone")
        return await _run(
            argv=(
                "git", "clone",
                "--branch", branch,
                "--single-branch",
                project["repo_url"],
                ".",
            ),
            shell_cmd=None,
            cwd=deploy_path,
            timeout=GIT_TIMEOUT_SECONDS,
            sink=sink,
        )
    sink.section("error")
    sink.write(f"Deploy path exists but is not a git repository: {deploy_path}")
    return StepResult(succeeded=False)


async def _run_step(step: dict, deploy_path: Path, sink: _Sink) -> StepResult:
    command = step["command"]
    if step["use_shell"]:
        return await _run(
            argv=None,
            shell_cmd=command,
            cwd=deploy_path,
            timeout=STEP_TIMEOUT_SECONDS,
            sink=sink,
        )
    argv = tuple(shlex.split(command))
    if not argv:
        sink.write(f"error: step '{step['name']}' has empty command")
        return StepResult(succeeded=False)
    return await _run(
        argv=argv,
        shell_cmd=None,
        cwd=deploy_path,
        timeout=STEP_TIMEOUT_SECONDS,
        sink=sink,
    )


async def run_deploy(
    project: dict,
    log_id: int,
    commit_sha: str | None = None,
    commit_message: str | None = None,
) -> None:
    log.info("Deploy queued: project=%s log_id=%d", project["name"], log_id)
    sink = _Sink(log_id)

    async with _project_lock(project["id"]):
        log.info("Deploy started: project=%s log_id=%d", project["name"], log_id)
        deploy_path = Path(project["deploy_path"]).expanduser()

        sink.section("Deploy Context")
        sink.write(f"project: {project['name']}")
        sink.write(f"deploy_path: {deploy_path}")
        sink.write(f"branch: {project['branch']}")
        sink.write(f"commit_sha: {commit_sha or '-'}")
        sink.write(f"commit_message: {commit_message or 'Manual deploy'}")

        status = "success"
        try:
            git_result = await _ensure_source(project, deploy_path, sink)
            if not git_result.succeeded:
                status = "failed"
            else:
                steps = await db.get_enabled_steps(project["id"])
                if not steps:
                    sink.section("Pipeline")
                    sink.write("활성화된 단계가 없습니다. 프로젝트 설정에서 단계를 추가하세요.")
                    status = "failed"
                else:
                    for index, step in enumerate(steps, start=1):
                        sink.section(f"step {index}: {step['name']}")
                        result = await _run_step(step, deploy_path, sink)
                        if not result.succeeded:
                            status = "failed"
                            break
        except Exception as exc:
            sink.section("error")
            sink.write(str(exc))
            status = "failed"

        sink.section(f"deploy: {status}")

        await db.finish_deploy_log(log_id, status, sink.text())
        broker.publish_done(log_id, status)
        log.info("Deploy finished: project=%s status=%s", project["name"], status)
