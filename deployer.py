from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import database as db

log = logging.getLogger("deploy-hook")


async def _run_command(*args: str, cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    output = stdout.decode(errors="replace") if stdout else ""
    return proc.returncode, output


async def run_deploy(project: dict, commit_sha: str | None = None, commit_message: str | None = None) -> None:
    log_id = await db.create_deploy_log(project["id"], commit_sha, commit_message)
    log.info("Deploy started: project=%s log_id=%d", project["name"], log_id)

    deploy_path = Path(project["deploy_path"]).expanduser()
    branch = project["branch"]

    try:
        if not deploy_path.is_dir():
            raise FileNotFoundError(f"Deploy path does not exist: {deploy_path}")

        pull_code, pull_output = await _run_command("git", "pull", "origin", branch, cwd=deploy_path)
        compose_code, compose_output = (0, "")
        if pull_code == 0:
            compose_code, compose_output = await _run_command("docker", "compose", "up", "--build", "-d", cwd=deploy_path)

        output_parts = [part for part in (pull_output, compose_output) if part]
        output = "\n".join(output_parts).strip()
        if not output:
            output = "(no output)"
        status = "success" if pull_code == 0 and compose_code == 0 else "failed"
    except asyncio.TimeoutError:
        output = "Deploy timed out after 300 seconds"
        status = "failed"
    except Exception as exc:
        output = str(exc)
        status = "failed"

    await db.finish_deploy_log(log_id, status, output)
    log.info("Deploy finished: project=%s status=%s", project["name"], status)
