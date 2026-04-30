from __future__ import annotations

from contextlib import asynccontextmanager

import aiosqlite

from config import DATABASE_PATH, DEPLOY_LOG_MAX_BYTES, DEPLOY_LOG_RETENTION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    repo_url    TEXT    NOT NULL,
    deploy_path TEXT    NOT NULL,
    branch      TEXT    NOT NULL DEFAULT 'main',
    webhook_secret TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deploy_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    commit_sha     TEXT,
    commit_message TEXT,
    status         TEXT    NOT NULL DEFAULT 'running',
    output         TEXT,
    started_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    command     TEXT    NOT NULL,
    use_shell   INTEGER NOT NULL DEFAULT 1,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_steps_project ON steps(project_id, position);
"""

DEFAULT_STEP_NAME = "Build & Deploy"
DEFAULT_STEP_COMMAND = "docker compose up --build -d"


async def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("PRAGMA auto_vacuum")
        row = await cur.fetchone()
        current_mode = row[0] if row else 0
        if current_mode != 2:
            await db.execute("PRAGMA auto_vacuum = INCREMENTAL")
            await db.execute("VACUUM")
        await db.executescript(_SCHEMA)
        await db.execute(
            """
            INSERT INTO steps (project_id, position, name, command, use_shell, enabled)
            SELECT p.id, 1, ?, ?, 1, 1
            FROM projects p
            WHERE NOT EXISTS (SELECT 1 FROM steps s WHERE s.project_id = p.id)
            """,
            (DEFAULT_STEP_NAME, DEFAULT_STEP_COMMAND),
        )
        await db.commit()


def _truncate_output(output: str) -> str:
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= DEPLOY_LOG_MAX_BYTES:
        return output
    head = encoded[:DEPLOY_LOG_MAX_BYTES].decode("utf-8", errors="replace")
    return head + f"\n\n... (truncated; original output was {len(encoded)} bytes)"


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict:
    return {col[0]: val for col, val in zip(cursor.description, row)}


@asynccontextmanager
async def _conn():
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = _row_to_dict  # type: ignore[assignment]
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


# ── Projects ──────────────────────────────────────────────────────────

async def get_all_projects() -> list[dict]:
    async with _conn() as db:
        cur = await db.execute(
            """
            SELECT p.*,
                   dl.status AS last_status,
                   dl.finished_at AS last_deployed_at
            FROM projects p
            LEFT JOIN deploy_logs dl ON dl.id = (
                SELECT id FROM deploy_logs
                WHERE project_id = p.id
                ORDER BY id DESC LIMIT 1
            )
            ORDER BY p.id
            """
        )
        return await cur.fetchall()


async def get_project(project_id: int) -> dict | None:
    async with _conn() as db:
        cur = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        return await cur.fetchone()


async def get_project_by_repo(repo_url: str) -> dict | None:
    async with _conn() as db:
        cur = await db.execute("SELECT * FROM projects WHERE repo_url = ? AND enabled = 1", (repo_url,))
        return await cur.fetchone()


async def create_project(name: str, repo_url: str, deploy_path: str, branch: str, webhook_secret: str) -> int:
    async with _conn() as db:
        cur = await db.execute(
            "INSERT INTO projects (name, repo_url, deploy_path, branch, webhook_secret) VALUES (?, ?, ?, ?, ?)",
            (name, repo_url, deploy_path, branch, webhook_secret),
        )
        project_id: int = cur.lastrowid  # type: ignore[assignment]
        await db.execute(
            "INSERT INTO steps (project_id, position, name, command, use_shell, enabled) VALUES (?, 1, ?, ?, 1, 1)",
            (project_id, DEFAULT_STEP_NAME, DEFAULT_STEP_COMMAND),
        )
        await db.commit()
        return project_id


async def update_project(
    project_id: int,
    *,
    name: str,
    repo_url: str,
    deploy_path: str,
    branch: str,
    webhook_secret: str | None,
) -> None:
    async with _conn() as db:
        if webhook_secret:
            await db.execute(
                "UPDATE projects SET name=?, repo_url=?, deploy_path=?, branch=?, webhook_secret=? WHERE id=?",
                (name, repo_url, deploy_path, branch, webhook_secret, project_id),
            )
        else:
            await db.execute(
                "UPDATE projects SET name=?, repo_url=?, deploy_path=?, branch=? WHERE id=?",
                (name, repo_url, deploy_path, branch, project_id),
            )
        await db.commit()


async def delete_project(project_id: int) -> None:
    async with _conn() as db:
        await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await db.commit()


async def toggle_project(project_id: int) -> None:
    async with _conn() as db:
        await db.execute("UPDATE projects SET enabled = 1 - enabled WHERE id = ?", (project_id,))
        await db.commit()


# ── Deploy Logs ───────────────────────────────────────────────────────

async def create_deploy_log(project_id: int, commit_sha: str | None, commit_message: str | None) -> int:
    async with _conn() as db:
        cur = await db.execute(
            "INSERT INTO deploy_logs (project_id, commit_sha, commit_message) VALUES (?, ?, ?)",
            (project_id, commit_sha, commit_message),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def finish_deploy_log(log_id: int, status: str, output: str) -> None:
    output = _truncate_output(output)
    async with _conn() as db:
        await db.execute(
            "UPDATE deploy_logs SET status=?, output=?, finished_at=datetime('now') WHERE id=?",
            (status, output, log_id),
        )
        cur = await db.execute("SELECT project_id FROM deploy_logs WHERE id=?", (log_id,))
        row = await cur.fetchone()
        if row:
            project_id = row["project_id"]
            await db.execute(
                """
                DELETE FROM deploy_logs
                WHERE project_id = ?
                  AND id NOT IN (
                      SELECT id FROM deploy_logs
                      WHERE project_id = ?
                      ORDER BY id DESC LIMIT ?
                  )
                """,
                (project_id, project_id, DEPLOY_LOG_RETENTION),
            )
        await db.execute("PRAGMA incremental_vacuum")
        await db.commit()


async def get_deploy_logs(project_id: int, limit: int = 20) -> list[dict]:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT * FROM deploy_logs WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        return await cur.fetchall()


# ── Steps ─────────────────────────────────────────────────────────────

async def get_steps(project_id: int) -> list[dict]:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT * FROM steps WHERE project_id = ? ORDER BY position, id",
            (project_id,),
        )
        return await cur.fetchall()


async def get_enabled_steps(project_id: int) -> list[dict]:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT * FROM steps WHERE project_id = ? AND enabled = 1 ORDER BY position, id",
            (project_id,),
        )
        return await cur.fetchall()


async def get_step(step_id: int) -> dict | None:
    async with _conn() as db:
        cur = await db.execute("SELECT * FROM steps WHERE id = ?", (step_id,))
        return await cur.fetchone()


async def create_step(project_id: int, name: str, command: str, use_shell: bool) -> int:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM steps WHERE project_id = ?",
            (project_id,),
        )
        row = await cur.fetchone()
        position = row["pos"] if row else 1
        cur = await db.execute(
            "INSERT INTO steps (project_id, position, name, command, use_shell) VALUES (?, ?, ?, ?, ?)",
            (project_id, position, name, command, 1 if use_shell else 0),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def update_step(step_id: int, *, name: str, command: str, use_shell: bool) -> None:
    async with _conn() as db:
        await db.execute(
            "UPDATE steps SET name=?, command=?, use_shell=? WHERE id=?",
            (name, command, 1 if use_shell else 0, step_id),
        )
        await db.commit()


async def delete_step(step_id: int) -> None:
    async with _conn() as db:
        await db.execute("DELETE FROM steps WHERE id = ?", (step_id,))
        await db.commit()


async def toggle_step(step_id: int) -> None:
    async with _conn() as db:
        await db.execute("UPDATE steps SET enabled = 1 - enabled WHERE id = ?", (step_id,))
        await db.commit()


async def move_step(step_id: int, direction: str) -> None:
    """Swap position with the previous (up) or next (down) step in the same project."""
    if direction not in ("up", "down"):
        return
    async with _conn() as db:
        cur = await db.execute("SELECT id, project_id, position FROM steps WHERE id = ?", (step_id,))
        current = await cur.fetchone()
        if not current:
            return
        if direction == "up":
            cur = await db.execute(
                "SELECT id, position FROM steps WHERE project_id = ? AND position < ? "
                "ORDER BY position DESC, id DESC LIMIT 1",
                (current["project_id"], current["position"]),
            )
        else:
            cur = await db.execute(
                "SELECT id, position FROM steps WHERE project_id = ? AND position > ? "
                "ORDER BY position ASC, id ASC LIMIT 1",
                (current["project_id"], current["position"]),
            )
        neighbor = await cur.fetchone()
        if not neighbor:
            return
        await db.execute(
            "UPDATE steps SET position = ? WHERE id = ?",
            (neighbor["position"], current["id"]),
        )
        await db.execute(
            "UPDATE steps SET position = ? WHERE id = ?",
            (current["position"], neighbor["id"]),
        )
        await db.commit()
