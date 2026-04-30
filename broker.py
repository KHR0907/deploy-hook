"""In-memory pub/sub for live deploy log streaming.

Each deploy is keyed by its ``deploy_logs.id``. Producers (``deployer.run_deploy``)
call ``publish_line`` for every line and ``publish_done`` when the deploy ends.
Consumers (the SSE endpoint) call ``subscribe`` to receive future lines plus a
backlog of any lines emitted before they connected.

State is kept in process memory; for a single-process FastAPI server this is
sufficient. Buffers are evicted ~60s after a deploy completes so memory does
not grow without bound; late subscribers fall back to the DB-stored output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger("deploy-hook")

RETAIN_AFTER_DONE_SECONDS = 60
MAX_LINES_PER_LOG = 20_000
SUBSCRIBER_QUEUE_SIZE = 4096


class _LogState:
    __slots__ = ("lines", "subscribers", "done", "status")

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done: bool = False
        self.status: Optional[str] = None


_states: dict[int, _LogState] = {}


def _ensure(log_id: int) -> _LogState:
    state = _states.get(log_id)
    if state is None:
        state = _LogState()
        _states[log_id] = state
    return state


def publish_line(log_id: int, line: str) -> None:
    state = _ensure(log_id)
    if state.done:
        return
    if len(state.lines) >= MAX_LINES_PER_LOG:
        return
    state.lines.append(line)
    index = len(state.lines)
    for q in list(state.subscribers):
        try:
            q.put_nowait(("line", line, index))
        except asyncio.QueueFull:
            log.warning("broker: subscriber queue full for log %s", log_id)


def publish_done(log_id: int, status: str) -> None:
    state = _ensure(log_id)
    if state.done:
        return
    state.done = True
    state.status = status
    for q in list(state.subscribers):
        try:
            q.put_nowait(("done", status, 0))
        except asyncio.QueueFull:
            pass
    asyncio.create_task(_evict_after(log_id, RETAIN_AFTER_DONE_SECONDS))


async def _evict_after(log_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    _states.pop(log_id, None)


def subscribe(log_id: int, since: int = 0) -> Optional[tuple[asyncio.Queue, list[tuple[str, str, int]]]]:
    """Subscribe to a live deploy. Returns ``None`` if no in-memory state exists
    (i.e. the deploy was never streamed in this process or has been evicted)."""
    state = _states.get(log_id)
    if state is None:
        return None
    q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
    state.subscribers.add(q)
    backlog: list[tuple[str, str, int]] = []
    for index, line in enumerate(state.lines[since:], start=since + 1):
        backlog.append(("line", line, index))
    if state.done:
        backlog.append(("done", state.status or "completed", 0))
    return q, backlog


def unsubscribe(log_id: int, q: asyncio.Queue) -> None:
    state = _states.get(log_id)
    if state:
        state.subscribers.discard(q)


def is_active(log_id: int) -> bool:
    state = _states.get(log_id)
    return state is not None and not state.done
