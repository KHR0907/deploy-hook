from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import database as db

log = logging.getLogger("deploy-hook")
GIT_TIMEOUT_SECONDS = 300
COMPOSE_TIMEOUT_SECONDS = 1800
DIAGNOSTIC_LOG_LINES = 200


@dataclass(slots=True)
class CommandResult:
    args: tuple[str, ...]
    cwd: Path
    duration_seconds: float
    output: str
    returncode: int | None
    timed_out: bool
    timeout_seconds: int


def _render_section(title: str, body: str) -> str:
    return f"=== {title} ===\n{body.strip()}"


def _render_command(result: CommandResult) -> str:
    lines = [
        f"$ (cd {shlex.quote(str(result.cwd))} && {shlex.join(result.args)})",
        f"duration: {result.duration_seconds:.1f}s",
    ]
    if result.timed_out:
        lines.append(f"status: timed out after {result.timeout_seconds}s")
    else:
        lines.append(f"exit_code: {result.returncode}")
    lines.append("")
    lines.append(result.output.strip() or "(no output)")
    return "\n".join(lines)


def _compose_file_exists(deploy_path: Path) -> bool:
    return any((deploy_path / name).is_file() for name in ("docker-compose.yml", "compose.yml", "compose.yaml"))


def _is_git_repo(deploy_path: Path) -> bool:
    return (deploy_path / ".git").exists()


def _is_directory_empty(path: Path) -> bool:
    return not any(path.iterdir())


def _compose_output_has_container_error(output: str) -> bool:
    lowered = output.lower()
    error_markers = (" exited ", " restarting ", " dead ", " created ", " removal in progress ")
    return any(marker in lowered for marker in error_markers)


async def _run_command(*args: str, cwd: Path, timeout_seconds: int) -> CommandResult:
    started_at = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    timed_out = False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace") if stdout else ""
    return CommandResult(
        args=args,
        cwd=cwd,
        duration_seconds=time.monotonic() - started_at,
        output=output,
        returncode=None if timed_out else proc.returncode,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
    )


async def _resolve_compose_command(cwd: Path) -> tuple[tuple[str, ...], str]:
    docker_path = shutil.which("docker")
    if docker_path:
        docker_result = await _run_command(docker_path, "compose", "version", cwd=cwd, timeout_seconds=15)
        if not docker_result.timed_out and docker_result.returncode == 0:
            return (docker_path, "compose"), _render_section("docker compose version", _render_command(docker_result))

    docker_compose_path = shutil.which("docker-compose")
    if docker_compose_path:
        compose_result = await _run_command(docker_compose_path, "version", cwd=cwd, timeout_seconds=15)
        if not compose_result.timed_out and compose_result.returncode == 0:
            return (docker_compose_path,), _render_section("docker-compose version", _render_command(compose_result))

    raise FileNotFoundError(
        "Docker CLI is not available in deploy-hook runtime. Rebuild the deploy-hook image with docker/docker-compose installed."
    )


async def run_deploy(project: dict, commit_sha: str | None = None, commit_message: str | None = None) -> None:
    log_id = await db.create_deploy_log(project["id"], commit_sha, commit_message)
    log.info("Deploy started: project=%s log_id=%d", project["name"], log_id)

    deploy_path = Path(project["deploy_path"]).expanduser()
    branch = project["branch"]

    try:
        output_sections = [
            _render_section(
                "Deploy Context",
                "\n".join(
                    [
                        f"project: {project['name']}",
                        f"deploy_path: {deploy_path}",
                        f"branch: {branch}",
                        f"commit_sha: {commit_sha or '-'}",
                        f"commit_message: {commit_message or 'Manual deploy'}",
                    ]
                ),
            )
        ]

        clone_result: CommandResult | None = None
        pull_result: CommandResult | None = None

        if not deploy_path.exists():
            deploy_path.parent.mkdir(parents=True, exist_ok=True)
            clone_result = await _run_command(
                "git",
                "clone",
                "--branch",
                branch,
                "--single-branch",
                project["repo_url"],
                str(deploy_path),
                cwd=deploy_path.parent,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
            output_sections.append(_render_section("git clone", _render_command(clone_result)))
        elif not deploy_path.is_dir():
            raise FileNotFoundError(f"Deploy path is not a directory: {deploy_path}")
        elif _is_git_repo(deploy_path):
            pull_result = await _run_command(
                "git",
                "-c",
                f"safe.directory={deploy_path}",
                "pull",
                "origin",
                branch,
                cwd=deploy_path,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
            output_sections.append(_render_section("git pull", _render_command(pull_result)))
        elif _is_directory_empty(deploy_path):
            clone_result = await _run_command(
                "git",
                "clone",
                "--branch",
                branch,
                "--single-branch",
                project["repo_url"],
                ".",
                cwd=deploy_path,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
            output_sections.append(_render_section("git clone", _render_command(clone_result)))
        else:
            raise FileNotFoundError(f"Deploy path exists but is not a git repository: {deploy_path}")

        if not deploy_path.is_dir():
            raise FileNotFoundError(f"Deploy path does not exist after git step: {deploy_path}")
        if not _compose_file_exists(deploy_path):
            raise FileNotFoundError(f"Compose file not found in deploy path: {deploy_path}")

        compose_command, compose_version_section = await _resolve_compose_command(deploy_path)
        output_sections.append(compose_version_section)

        compose_config_result: CommandResult | None = None
        compose_up_result: CommandResult | None = None
        compose_ps_result: CommandResult | None = None
        compose_logs_result: CommandResult | None = None

        git_step_failed = False
        if clone_result and (clone_result.timed_out or clone_result.returncode != 0):
            git_step_failed = True
        if pull_result and (pull_result.timed_out or pull_result.returncode != 0):
            git_step_failed = True

        if not git_step_failed:
            compose_config_result = await _run_command(
                *compose_command,
                "config",
                cwd=deploy_path,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
            output_sections.append(_render_section("compose config", _render_command(compose_config_result)))

        if compose_config_result and not compose_config_result.timed_out and compose_config_result.returncode == 0:
            compose_up_result = await _run_command(
                *compose_command,
                "up",
                "--build",
                "-d",
                cwd=deploy_path,
                timeout_seconds=COMPOSE_TIMEOUT_SECONDS,
            )
            output_sections.append(_render_section("compose up --build -d", _render_command(compose_up_result)))

        if compose_up_result:
            compose_ps_result = await _run_command(
                *compose_command,
                "ps",
                "-a",
                cwd=deploy_path,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
            output_sections.append(_render_section("compose ps -a", _render_command(compose_ps_result)))

            compose_logs_result = await _run_command(
                *compose_command,
                "logs",
                "--timestamps",
                "--tail",
                str(DIAGNOSTIC_LOG_LINES),
                cwd=deploy_path,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
            output_sections.append(_render_section("compose logs", _render_command(compose_logs_result)))

        status = "success"
        if clone_result and (clone_result.timed_out or clone_result.returncode != 0):
            status = "failed"
        elif pull_result and (pull_result.timed_out or pull_result.returncode != 0):
            status = "failed"
        elif compose_config_result is None or compose_config_result.timed_out or compose_config_result.returncode != 0:
            status = "failed"
        elif compose_up_result is None or compose_up_result.timed_out or compose_up_result.returncode != 0:
            status = "failed"
        elif compose_ps_result and _compose_output_has_container_error(compose_ps_result.output):
            status = "failed"

        output = "\n\n".join(output_sections)
    except Exception as exc:
        output = str(exc)
        status = "failed"

    await db.finish_deploy_log(log_id, status, output)
    log.info("Deploy finished: project=%s status=%s", project["name"], status)
