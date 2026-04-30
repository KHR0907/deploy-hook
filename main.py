from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import broker
import config
import database as db
from deployer import run_deploy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("deploy-hook")

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    same_site="strict",
    https_only=config.SESSION_HTTPS_ONLY,
    max_age=60 * 60 * 12,
)
templates = Jinja2Templates(directory="templates")

_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_MIN_SECRET_KEY_LEN = 32
_MIN_ADMIN_PASSWORD_LEN = 12
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_LOCK_SECONDS = 15 * 60
_MAX_FAILED_LOGINS = 5
_FAILED_LOGINS: dict[str, list[float]] = {}
_LOCKED_UNTIL: dict[str, float] = {}


# ── Helpers ───────────────────────────────────────────────────────────

def _is_logged_in(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _require_login(request: Request) -> RedirectResponse | None:
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _set_notice(request: Request, level: str, message: str) -> None:
    request.session["notice"] = {"level": level, "message": message}


def _pop_notice(request: Request) -> dict | None:
    return request.session.pop("notice", None)


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _require_csrf(request: Request, csrf_token: str) -> RedirectResponse | None:
    expected = request.session.get("csrf_token")
    if not expected or not hmac.compare_digest(expected, csrf_token):
        _set_notice(request, "error", "잘못된 요청입니다. 페이지를 새로고침한 뒤 다시 시도하세요.")
        target = "/login" if not _is_logged_in(request) else "/"
        return RedirectResponse(target, status_code=303)
    return None


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _prune_failures(ip: str, now: float) -> None:
    attempts = _FAILED_LOGINS.get(ip, [])
    attempts = [attempt for attempt in attempts if now - attempt <= _LOGIN_WINDOW_SECONDS]
    if attempts:
        _FAILED_LOGINS[ip] = attempts
    else:
        _FAILED_LOGINS.pop(ip, None)


def _is_login_blocked(ip: str, now: float) -> bool:
    locked_until = _LOCKED_UNTIL.get(ip, 0.0)
    if locked_until > now:
        return True
    _LOCKED_UNTIL.pop(ip, None)
    return False


def _record_login_failure(ip: str, now: float) -> None:
    _prune_failures(ip, now)
    attempts = _FAILED_LOGINS.setdefault(ip, [])
    attempts.append(now)
    if len(attempts) >= _MAX_FAILED_LOGINS:
        _LOCKED_UNTIL[ip] = now + _LOGIN_LOCK_SECONDS
        _FAILED_LOGINS.pop(ip, None)


def _clear_login_failures(ip: str) -> None:
    _FAILED_LOGINS.pop(ip, None)
    _LOCKED_UNTIL.pop(ip, None)


def _validate_branch(branch: str) -> str:
    value = branch.strip()
    if not value or not _SAFE_BRANCH_RE.fullmatch(value):
        raise ValueError("브랜치 이름 형식이 올바르지 않습니다.")
    if ".." in value or value.endswith("/") or "/." in value or "@{" in value:
        raise ValueError("브랜치 이름 형식이 올바르지 않습니다.")
    return value


def _validate_deploy_path(deploy_path: str) -> str:
    value = deploy_path.strip()
    if not value:
        raise ValueError("배포 경로를 입력하세요.")
    if any(char in value for char in ("\x00", "\n", "\r")):
        raise ValueError("배포 경로에 허용되지 않는 문자가 포함되어 있습니다.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("배포 경로는 절대 경로여야 합니다.")
    return str(path)


def _validate_webhook_secret(secret: str) -> str:
    value = secret.strip()
    if len(value) < 16:
        raise ValueError("Webhook secret은 16자 이상이어야 합니다.")
    return value


def _validate_step_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise ValueError("단계 이름을 입력하세요.")
    if len(value) > 80:
        raise ValueError("단계 이름이 너무 깁니다.")
    return value


def _validate_step_command(command: str) -> str:
    value = command.strip()
    if not value:
        raise ValueError("실행 명령을 입력하세요.")
    if any(ch in value for ch in ("\x00",)):
        raise ValueError("명령에 허용되지 않는 문자가 포함되어 있습니다.")
    return value


def _project_template_context(request: Request, **extra: object) -> dict[str, object]:
    context: dict[str, object] = {"request": request, "csrf_token": _get_csrf_token(request)}
    context.update(extra)
    return context


# ── Startup ───────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await db.init_db()
    if config.SECRET_KEY == "change-me-in-production" or len(config.SECRET_KEY) < _MIN_SECRET_KEY_LEN:
        log.warning("SECRET_KEY is weak. Rotate it before exposing this service.")
    if len(config.ADMIN_PASSWORD) < _MIN_ADMIN_PASSWORD_LEN:
        log.warning("ADMIN_PASSWORD is weak. Rotate it before exposing this service.")
    log.info("deploy-hook started on port %d", config.PORT)


# ── Health ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Auth ──────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", _project_template_context(request, error=None))


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...), csrf_token: str = Form(...)):
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    if not config.ADMIN_PASSWORD:
        _set_notice(request, "error", "ADMIN_PASSWORD가 설정되지 않았습니다.")
        return RedirectResponse("/login", status_code=303)
    now = time.time()
    ip = _client_ip(request)
    if _is_login_blocked(ip, now):
        return templates.TemplateResponse(
            "login.html",
            _project_template_context(request, error="로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요."),
            status_code=429,
        )
    if not hmac.compare_digest(password, config.ADMIN_PASSWORD):
        _record_login_failure(ip, now)
        return templates.TemplateResponse(
            "login.html",
            _project_template_context(request, error="비밀번호가 올바르지 않습니다."),
            status_code=401,
        )
    _clear_login_failures(ip)
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── Dashboard ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if redirect := _require_login(request):
        return redirect
    projects = await db.get_all_projects()
    return templates.TemplateResponse(
        "dashboard.html",
        _project_template_context(request, projects=projects, notice=_pop_notice(request)),
    )


# ── Project CRUD ──────────────────────────────────────────────────────

@app.post("/projects")
async def create_project(
    request: Request,
    name: str = Form(...),
    repo_url: str = Form(...),
    deploy_path: str = Form(...),
    branch: str = Form("main"),
    webhook_secret: str = Form(...),
    csrf_token: str = Form(...),
):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    try:
        await db.create_project(
            name.strip(),
            repo_url.strip(),
            _validate_deploy_path(deploy_path),
            _validate_branch(branch),
            _validate_webhook_secret(webhook_secret),
        )
        _set_notice(request, "success", f"프로젝트 '{name}'이 추가되었습니다.")
    except Exception as exc:
        _set_notice(request, "error", str(exc))
    return RedirectResponse("/", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: int):
    if redirect := _require_login(request):
        return redirect
    project = await db.get_project(project_id)
    if not project:
        _set_notice(request, "error", "프로젝트를 찾을 수 없습니다.")
        return RedirectResponse("/", status_code=303)
    logs = await db.get_deploy_logs(project_id)
    steps = await db.get_steps(project_id)
    return templates.TemplateResponse(
        "project.html",
        _project_template_context(
            request,
            project=project,
            logs=logs,
            steps=steps,
            notice=_pop_notice(request),
        ),
    )


@app.post("/projects/{project_id}/update")
async def update_project(
    request: Request,
    project_id: int,
    name: str = Form(...),
    repo_url: str = Form(...),
    deploy_path: str = Form(...),
    branch: str = Form("main"),
    webhook_secret: str = Form(""),
    csrf_token: str = Form(...),
):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    try:
        secret = _validate_webhook_secret(webhook_secret) if webhook_secret.strip() else None
        await db.update_project(
            project_id,
            name=name.strip(),
            repo_url=repo_url.strip(),
            deploy_path=_validate_deploy_path(deploy_path),
            branch=_validate_branch(branch),
            webhook_secret=secret,
        )
        _set_notice(request, "success", "프로젝트가 수정되었습니다.")
    except Exception as exc:
        _set_notice(request, "error", str(exc))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/delete")
async def delete_project(request: Request, project_id: int, csrf_token: str = Form(...)):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    await db.delete_project(project_id)
    _set_notice(request, "success", "프로젝트가 삭제되었습니다.")
    return RedirectResponse("/", status_code=303)


@app.post("/projects/{project_id}/toggle")
async def toggle_project(request: Request, project_id: int, csrf_token: str = Form(...)):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    await db.toggle_project(project_id)
    _set_notice(request, "success", "프로젝트 상태가 변경되었습니다.")
    return RedirectResponse("/", status_code=303)


@app.post("/projects/{project_id}/deploy")
async def manual_deploy(request: Request, project_id: int, csrf_token: str = Form(...)):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    project = await db.get_project(project_id)
    if not project:
        _set_notice(request, "error", "프로젝트를 찾을 수 없습니다.")
        return RedirectResponse("/", status_code=303)
    log_id = await db.create_deploy_log(project_id, None, "Manual deploy")
    asyncio.create_task(run_deploy(project, log_id, commit_sha=None, commit_message="Manual deploy"))
    _set_notice(request, "success", f"'{project['name']}' 배포가 시작되었습니다.")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ── Steps (Pipeline) ──────────────────────────────────────────────────

def _form_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "on", "yes"}


@app.post("/projects/{project_id}/steps")
async def create_step(
    request: Request,
    project_id: int,
    name: str = Form(...),
    command: str = Form(...),
    use_shell: str = Form(""),
    csrf_token: str = Form(...),
):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    project = await db.get_project(project_id)
    if not project:
        _set_notice(request, "error", "프로젝트를 찾을 수 없습니다.")
        return RedirectResponse("/", status_code=303)
    try:
        await db.create_step(
            project_id,
            name=_validate_step_name(name),
            command=_validate_step_command(command),
            use_shell=_form_bool(use_shell),
        )
        _set_notice(request, "success", "단계가 추가되었습니다.")
    except Exception as exc:
        _set_notice(request, "error", str(exc))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/steps/{step_id}/update")
async def update_step(
    request: Request,
    project_id: int,
    step_id: int,
    name: str = Form(...),
    command: str = Form(...),
    use_shell: str = Form(""),
    csrf_token: str = Form(...),
):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    step = await db.get_step(step_id)
    if not step or step["project_id"] != project_id:
        _set_notice(request, "error", "단계를 찾을 수 없습니다.")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    try:
        await db.update_step(
            step_id,
            name=_validate_step_name(name),
            command=_validate_step_command(command),
            use_shell=_form_bool(use_shell),
        )
        _set_notice(request, "success", "단계가 수정되었습니다.")
    except Exception as exc:
        _set_notice(request, "error", str(exc))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/steps/{step_id}/delete")
async def delete_step(request: Request, project_id: int, step_id: int, csrf_token: str = Form(...)):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    step = await db.get_step(step_id)
    if step and step["project_id"] == project_id:
        await db.delete_step(step_id)
        _set_notice(request, "success", "단계가 삭제되었습니다.")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/steps/{step_id}/toggle")
async def toggle_step(request: Request, project_id: int, step_id: int, csrf_token: str = Form(...)):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    step = await db.get_step(step_id)
    if step and step["project_id"] == project_id:
        await db.toggle_step(step_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/steps/{step_id}/move")
async def move_step(
    request: Request,
    project_id: int,
    step_id: int,
    direction: str = Form(...),
    csrf_token: str = Form(...),
):
    if redirect := _require_login(request):
        return redirect
    if redirect := _require_csrf(request, csrf_token):
        return redirect
    step = await db.get_step(step_id)
    if step and step["project_id"] == project_id and direction in ("up", "down"):
        await db.move_step(step_id, direction)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ── Live log stream (SSE) ─────────────────────────────────────────────

def _format_sse(event_type: str, data: str, event_id: int) -> str:
    parts: list[str] = []
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append(f"event: {event_type}")
    for line in data.split("\n"):
        parts.append(f"data: {line}")
    return "\n".join(parts) + "\n\n"


@app.get("/projects/{project_id}/logs/{log_id}/stream")
async def stream_log(request: Request, project_id: int, log_id: int):
    if not _is_logged_in(request):
        return Response(status_code=401)
    deploy_log = await db.get_deploy_log(log_id)
    if not deploy_log or deploy_log["project_id"] != project_id:
        return Response(status_code=404)

    last_event_id = 0
    raw = request.headers.get("last-event-id")
    if raw and raw.isdigit():
        last_event_id = int(raw)

    async def event_stream():
        sub = broker.subscribe(log_id, since=last_event_id)
        if sub is None:
            # No live state — replay persisted output if the deploy has finished.
            if deploy_log["status"] != "running":
                lines = (deploy_log["output"] or "").split("\n")
                for n, line in enumerate(lines, start=1):
                    if n > last_event_id:
                        yield _format_sse("line", line, n)
                yield _format_sse("done", deploy_log["status"], 0)
            else:
                yield _format_sse("done", "stream_unavailable", 0)
            return

        q, backlog = sub
        try:
            for kind, payload, idx in backlog:
                yield _format_sse(kind, payload, idx)
                if kind == "done":
                    return
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                kind, payload, idx = msg
                yield _format_sse(kind, payload, idx)
                if kind == "done":
                    return
        finally:
            broker.unsubscribe(log_id, q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Webhook ───────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"status": "pong"}
    if event != "push":
        return Response(status_code=204)

    body = await request.body()
    payload = await request.json()

    repo_url = payload.get("repository", {}).get("html_url", "")
    if not repo_url:
        return Response(status_code=400)

    project = await db.get_project_by_repo(repo_url)
    if not project:
        return Response(status_code=404)

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(project["webhook_secret"], body, signature):
        log.warning("Invalid webhook signature for project=%s", project["name"])
        return Response(status_code=403)

    # Branch filter
    ref = payload.get("ref", "")
    if ref != f"refs/heads/{project['branch']}":
        return {"status": "skipped", "reason": "branch mismatch"}

    # Extract commit info
    head_commit = payload.get("head_commit", {})
    commit_sha = head_commit.get("id", "")[:8]
    commit_message = head_commit.get("message", "")

    log_id = await db.create_deploy_log(project["id"], commit_sha, commit_message)
    asyncio.create_task(run_deploy(project, log_id, commit_sha, commit_message))
    log.info("Webhook deploy triggered: project=%s commit=%s", project["name"], commit_sha)
    return {"status": "deploying"}


# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
