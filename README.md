# deploy-hook

GitHub webhook 또는 대시보드 버튼으로 서버의 프로젝트를 배포할 수 있는 FastAPI 기반 관리 도구입니다.

## 기능

- 관리자 로그인 기반 대시보드
- 프로젝트별 GitHub webhook 배포
- 대시보드에서 수동 배포 버튼 지원
- 배포 이력 및 출력 로그 확인
- SQLite 기반 경량 저장소

## 요구 사항

- Python 3.11+ 또는 Docker
- Docker / Docker Compose

## 환경 변수

`.env.example`을 복사해서 `.env`를 만듭니다.

```bash
cp .env.example .env
```

주요 설정:

- `ADMIN_PASSWORD`: 관리자 로그인 비밀번호
- `SECRET_KEY`: 세션 서명용 랜덤 키
- `PORT`: 앱 포트
- `DATABASE_PATH`: SQLite 파일 경로
- `SESSION_HTTPS_ONLY`: HTTPS 환경이면 `true`

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

접속 주소:

- `http://localhost:9000/login`

## Docker 실행

```bash
docker compose up --build -d
```

상태 확인:

```bash
curl http://127.0.0.1:9000/health
```

정상 응답:

```json
{"status":"ok"}
```

## 서버 배포

예시:

```bash
cd /srv
git clone https://github.com/KHR0907/deploy-hook.git
cd deploy-hook
cp .env.example .env
docker compose up --build -d
```

운영에서는 앱 포트를 외부에 직접 노출하기보다 Nginx 또는 Caddy 뒤에 두고 HTTPS로 서비스하는 편이 안전합니다.

## 사용 방법

1. 로그인합니다.
2. 프로젝트를 추가합니다.
3. 아래 값을 입력합니다.
   - `프로젝트 이름`
   - `GitHub 레포 URL`
   - `서버 배포 경로`
   - `브랜치`
   - `Webhook Secret`
4. GitHub webhook을 연결하거나, 대시보드의 `지금 배포` 버튼으로 수동 배포합니다.

## 배포 방식

배포 시 서버에서 아래 명령이 실행됩니다.

```bash
git clone --branch <branch> <repo-url> <deploy_path>   # 경로가 없거나 비어 있으면
git pull origin <branch>                               # 이미 clone 되어 있으면
docker compose up --build -d
docker compose ps -a
docker compose logs --tail <N>
```

즉, `서버 배포 경로`는 다음 둘 중 하나면 됩니다.

- 아직 없는 경로 또는 비어 있는 디렉터리
- 이미 Git 저장소와 `docker-compose.yml`이 있는 프로젝트 디렉터리

## GitHub Webhook 설정

GitHub 저장소에서 webhook을 추가합니다.

- Payload URL: `https://your-domain.com/webhook`
- Content type: `application/json`
- Secret: 대시보드에 등록한 `Webhook Secret`
- Events: `Just the push event`

브랜치가 프로젝트 설정과 일치할 때만 배포됩니다.

## 수동 배포

대시보드 프로젝트 목록 또는 프로젝트 상세 페이지에서 수동 배포를 실행할 수 있습니다.

## 보안 메모

- `SESSION_HTTPS_ONLY=true`로 운영하는 것을 권장합니다.
- `ADMIN_PASSWORD`와 `SECRET_KEY`는 충분히 긴 랜덤 값으로 설정해야 합니다.
- 이 앱은 Docker 소켓을 사용하므로, 신뢰할 수 있는 관리자만 접근해야 합니다.
- `.env`는 Git에 포함하지 않아야 합니다.
