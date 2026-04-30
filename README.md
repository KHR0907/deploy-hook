# deploy-hook

대시보드에서 버튼 한 번으로 서버 배포를 실행할 수 있는 FastAPI 기반 셀프호스팅 CI/CD 도구입니다.
프로젝트마다 **파이프라인(단계 목록)** 을 정의해두면, GitHub push 또는 수동 배포 시 단계가 위에서 아래로 순차 실행됩니다.

## 핵심 개념

- **단계(Step)** — 한 줄짜리 셸 명령. 프로젝트마다 여러 개를 순서대로 정의합니다.
- **파이프라인** — 한 프로젝트의 활성화된 단계들을 위에서 아래로 실행. 한 단계라도 실패하면 즉시 중단됩니다.
- **첫 단계는 항상 `git pull`(또는 최초 시 `git clone`)** — 자동 실행되며 사용자가 정의할 필요 없습니다.

## 기능

- 관리자 로그인 기반 대시보드
- 프로젝트별 파이프라인 정의 (UI에서 추가/수정/삭제/순서 변경/활성화 토글)
- GitHub Webhook 또는 대시보드 버튼으로 배포 트리거
- 프로젝트별 동시 배포 방지 (asyncio Lock)
- 단계별 출력 로그를 섹션 단위로 저장
- SQLite 기반 경량 저장소

## 요구 사항

- Docker / Docker Compose (권장)
- 또는 Python 3.11+

## 환경 변수

`.env.example`을 복사해서 `.env`를 만듭니다.

```bash
cp .env.example .env
```

| 변수 | 설명 |
|---|---|
| `ADMIN_PASSWORD` | 관리자 로그인 비밀번호 (12자 이상 권장) |
| `SECRET_KEY` | 세션 서명용 랜덤 키 (32자 이상) |
| `PORT` | 앱 포트 (기본 9000) |
| `DATABASE_PATH` | SQLite 파일 경로 (기본 `data/deploy-hook.db`) |
| `SESSION_HTTPS_ONLY` | HTTPS 환경에서 `true` 권장 |
| `DEPLOY_LOG_RETENTION` | 프로젝트별로 보관할 최근 배포 로그 개수 (기본 50, 초과분 자동 삭제) |
| `DEPLOY_LOG_MAX_BYTES` | 단일 배포 로그의 최대 크기 (기본 262144, 초과분은 잘리고 안내 메시지 추가) |

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

접속: `http://localhost:9000/login`

## Docker 실행

```bash
docker compose up --build -d
```

상태 확인:

```bash
curl http://127.0.0.1:9000/health
# {"status":"ok"}
```

## 서버 배포 예시

```bash
cd /srv
git clone https://github.com/KHR0907/deploy-hook.git
cd deploy-hook
cp .env.example .env
docker compose up --build -d
```

운영에서는 Nginx / Caddy 뒤에 두고 HTTPS로 서비스하세요.

## 사용 방법

1. 로그인합니다.
2. **프로젝트 추가** — 다음을 입력:
   - `프로젝트 이름`
   - `GitHub 레포 URL`
   - `서버 배포 경로` (절대 경로)
   - `브랜치`
   - `Webhook Secret` (16자 이상)
3. 새로 만든 프로젝트에는 기본 단계(`Build & Deploy` = `docker compose up --build -d`)가 자동으로 들어가 있습니다. 필요에 맞게 단계를 추가/수정하세요.
4. **`수동 배포` 버튼**을 누르거나, GitHub webhook을 연결해서 자동으로 트리거합니다.

### 파이프라인 예시

```
1. install         npm ci                          [shell]
2. test            npm test                        [shell]
3. build           npm run build                   [shell]
4. deploy          docker compose up --build -d    [shell]
5. health          curl -fsS http://localhost/healthz   [shell]
```

각 단계는:

- **이름** — UI 표시용
- **명령** — 한 줄 셸 명령
- **셸로 실행** 체크 — `&&`, 파이프(`|`), 리다이렉션(`>`) 등 사용 시 켜둡니다 (기본 ON)
- **활성/비활성** — 비활성 상태면 실행에서 제외
- **순서** — 위/아래 화살표로 변경

## 배포 흐름

배포가 실행되면 서버에서 다음 순서로 동작합니다:

1. 배포 경로에 따라 분기:
   - 경로가 없거나 비어 있으면 → `git clone --branch <branch> --single-branch <repo> <deploy_path>`
   - 이미 git repo면 → `git pull origin <branch>`
   - 그 외(파일이 있는 비-git 디렉터리) → 실패
2. 활성화된 단계를 위→아래 순으로 실행. `cwd`는 `deploy_path`.
3. 한 단계라도 실패(`exit_code != 0` 또는 timeout)하면 거기서 중단, 배포 상태 `failed`.
4. 모든 단계 통과 시 `success`.

각 단계 출력은 배포 이력의 단일 로그에 섹션 단위로 누적됩니다.

## GitHub Webhook 설정

GitHub 저장소 → Settings → Webhooks → Add webhook:

- **Payload URL**: `https://your-domain.com/webhook`
- **Content type**: `application/json`
- **Secret**: 대시보드에 등록한 `Webhook Secret`
- **Events**: `Just the push event`

브랜치가 프로젝트 설정과 일치할 때만 배포가 트리거됩니다. webhook을 연결하지 않으면 대시보드 버튼으로만 배포할 수 있습니다.

## 보안 메모

- `SESSION_HTTPS_ONLY=true`로 운영하세요.
- `ADMIN_PASSWORD`(12자+)와 `SECRET_KEY`(32자+)는 충분히 긴 랜덤 값을 사용하세요.
- 이 앱은 **Docker 소켓을 마운트**해서 호스트의 `docker compose`를 호출합니다. 사실상 호스트 root 권한이므로 신뢰된 관리자만 접근하도록 격리하세요.
- `docker-compose.yml`은 기본적으로 `/home/ubuntu`를 컨테이너에 마운트합니다. 다른 경로에 배포한다면 `docker-compose.yml`의 `volumes` 섹션을 환경에 맞게 수정하세요.
- 단계의 명령은 **셸 모드**로 실행되면 사용자가 입력한 그대로 실행됩니다. 관리자만 단계를 편집할 수 있지만, 잘못된 명령이 호스트에 영향을 줄 수 있으니 주의하세요.
- `.env`는 Git에 포함하지 마세요.

## 로그인 보호

- IP별 15분 윈도우, 5회 실패 시 15분 락아웃
- CSRF 토큰 검증
- 비밀번호 비교는 `hmac.compare_digest`
