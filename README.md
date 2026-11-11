# MyWay 메인 서버

서울 지하철 2·5·9호선 경로 안내 앱 **MyWay**의 메인 서버 (Python + FastAPI).
3계층 구조: 앱(Kotlin) → **메인 서버(이 저장소)** → AI 서비스(Python·FastAPI).
API 명세는 서버 실행 후 FastAPI /docs (http://127.0.0.1:8000/docs).

## 실행법

### Windows

```powershell
# 1. venv 생성 + 활성화
python -m venv .venv
.venv\Scripts\activate

# 2. 의존성 설치
pip install -r requirements.txt

# 3. 환경변수 파일 준비 (.env는 gitignore — 커밋 금지)
Copy-Item .env.example .env

# 4. DB 스키마 생성 + 데이터 적재
python scripts\init_db.py myway.db
python scripts\load_data.py myway.db data

# 5. 서버 실행
python main.py
```

(python 명령이 없다면 py -3 런처를 쓴다)

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env

.venv/bin/python scripts/init_db.py myway.db
.venv/bin/python scripts/load_data.py myway.db data

.venv/bin/python main.py
```

### 환경변수 (.env)

| 변수 | 값 |
| --- | --- |
| AI_SERVER_AUTH_TOKEN | AI 서버와 공유하는 Bearer 토큰. 생성: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| AI_SERVER_BASE_URL | AI 서버 주소. 로컬 시연 기준 http://127.0.0.1:8000 |

토큰 또는 base URL이 없거나 http/https가 아니면 서버 시작이 검증 단계에서 실패한다.

## 테스트

```bash
python -m pytest
```

mock AI 스텁으로 AI 서버 없이 단위 테스트를 돌린다.

## 구성

```
app/
  config.py     환경변수 설정 (시작 시 검증)
  db.py         SQLite 메인 DB (Station / RouteEdge / meta)
  catalog.py    역 카탈로그 (시작 시 DB→메모리)
  sessions.py   세션 조건 상태 (X-Session-ID, 늦은 응답 폐기)
  ai_client.py  메인→AI HTTP 클라이언트 (urllib, 재시도 0회)
  schemas.py    요청/응답 DTO
  routers/
    stations.py    역 목록·검색·resolve·confirm·release·swap
    interpret.py   자연어 해석 (AI 서비스 경유)
    conditions.py  조건 초기화·새 검색
  main.py       앱 팩토리 create_app()
scripts/
  init_db.py    스키마 생성
  load_data.py  전처리 산출물 CSV 적재 (필수 컬럼·참조 일치·수치 양수 검증)
data/           전처리 산출물 (2·5호선) + 임시 manifest.json
tests/          단위 테스트
```

※ 경로 탐색 API(POST /v1/routes/search)는 아직 미구현 — 응답 DTO는 app/schemas.py 참고
