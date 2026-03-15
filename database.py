from __future__ import annotations

import aiosqlite

from config import DATABASE_PATH

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
"""


async def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict:
    return {col[0]: val for col, val in zip(cursor.description, row)}


from contextlib import asynccontextmanager


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
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


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
    async with _conn() as db:
        await db.execute(
            "UPDATE deploy_logs SET status=?, output=?, finished_at=datetime('now') WHERE id=?",
            (status, output, log_id),
        )
        await db.commit()


async def get_deploy_logs(project_id: int, limit: int = 20) -> list[dict]:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT * FROM deploy_logs WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        return await cur.fetchall()
