import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# app.main 모듈 import 시 Settings/DB 생성에 필요한 환경변수 (토큰 값 자체는 로그에 남지 않음)
os.environ.setdefault("AI_SERVER_AUTH_TOKEN", "test-token-for-unit-tests")
os.environ.setdefault("AI_SERVER_BASE_URL", "http://127.0.0.1:8000")
os.environ.setdefault("MYWAY_EXPECTED_AI_PROVIDER", "mock")
os.environ.setdefault("MYWAY_DB_PATH", str(Path(tempfile.mkdtemp()) / "myway.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Database, init_db  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_TOKEN = "test-token-for-unit-tests"

TEST_ENV_BASE = {
    "AI_SERVER_AUTH_TOKEN": TEST_TOKEN,
    "AI_SERVER_BASE_URL": "http://127.0.0.1:8000",
    "MYWAY_EXPECTED_AI_PROVIDER": "mock",
    "MYWAY_ALGORITHM_SRC_PATH": os.environ.get(
        "MYWAY_ALGORITHM_SRC_PATH", str(ROOT.parent / "myway-algorithm" / "src")
    ),
}

# 앱 Fake와 같은 케이스: 동명 다른 노선 / 비활성 역 / 존재하지 않는 이름
STATION_FIXTURES = [
    # (station_id, group_id, name, line, is_active)
    ("L2_MAIN_001", "g_sicheong", "시청", "2", 1),
    ("L2_MAIN_003", "g_euljiro3", "을지로3가", "2", 1),
    ("L3_MAIN_003", "g_euljiro3", "을지로3가", "3", 1),  # 동명 다른 노선
    ("L5_539", "g_yeouido", "여의도", "5", 1),
    ("L9_930", "g_yeouido", "여의도", "9", 1),  # 동명 다른 노선
    ("L9_TEST_999", "g_inactive", "시험용비활성역", "9", 0),  # 비활성 역
]

# mock AI 기본 성공 conditions (AI 계약 snake_case)
DEFAULT_CONDITIONS = {
    "stations": {
        "origin": {"raw_name": "강남", "suggested_name": None},
        "destination": {"raw_name": "여의도", "suggested_name": None},
    },
    "constraints": {"max_duration_minutes": 40, "max_transfers": None, "max_fare_krw": None},
    "preference": None,
}


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    init_db(path)
    db = Database(path)
    for row in STATION_FIXTURES:
        db.execute(
            "INSERT INTO Station (station_id, group_id, name, line, is_active) VALUES (?, ?, ?, ?, ?)",
            row,
        )
    db.close()
    return path


@pytest.fixture
def client(db_path) -> TestClient:
    settings = Settings(env={**TEST_ENV_BASE, "MYWAY_DB_PATH": db_path})
    application = create_app(settings)
    with TestClient(application) as c:
        yield c


class MockAiHandler(BaseHTTPRequestHandler):
    """AI 서버 대신 쓰는 mock 스텁. POST /v1/conditions/interpret만 처리한다.

    mode: ok | business_error | server_error | bad_request | rate_limited | sleep
          | unauthorized | missing_exception | no_provider | wrong_provider
          | service_unavailable (503, error_code가 cfg에 있으면 본문에 포함)
    """

    def do_POST(self) -> None:
        cfg = self.server.mock_config
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        cfg["requests"].append(
            {"path": self.path, "body": body, "authorization": self.headers.get("Authorization")}
        )
        if self.path != "/v1/conditions/interpret":
            self._send(404, {"error": "not found"})
            return
        if cfg["mode"] == "unauthorized" or self.headers.get("Authorization") != f"Bearer {cfg['token']}":
            self._send(401, {"exception": True, "error_code": "AUTH_REQUIRED", "conditions": None})
            return
        mode = cfg["mode"]
        if mode == "sleep":
            time.sleep(cfg.get("sleep_seconds", 3))
        if mode == "server_error":
            self._send(500, {"exception": True, "error_code": "INTERNAL", "conditions": None})
            return
        if mode == "bad_request":
            # cfg에 error_code를 주면 덮어쓴다 (INPUT_LIMIT_EXCEEDED 등 422 세분 테스트용)
            self._send(
                422,
                {"exception": True, "error_code": cfg.get("error_code", "INVALID_REQUEST"), "conditions": None},
            )
            return
        if mode == "rate_limited":
            self._send(429, {"exception": True, "error_code": "RATE_LIMITED", "conditions": None})
            return
        if mode == "service_unavailable":
            obj = {"exception": True, "conditions": None}
            if cfg.get("error_code"):
                obj["error_code"] = cfg["error_code"]
            self._send(503, obj)
            return
        if mode == "business_error":
            self._send(
                200,
                {
                    "exception": True,
                    "error_code": cfg.get("error_code", "UNSUPPORTED_CONDITION"),
                    "conditions": None,
                },
            )
            return
        if mode == "missing_exception":
            self._send(200, {"error_code": None, "conditions": cfg.get("conditions", DEFAULT_CONDITIONS)})
            return
        # 성공
        self._send(
            200,
            {"exception": False, "error_code": None, "conditions": cfg.get("conditions", DEFAULT_CONDITIONS)},
            provider=cfg.get("provider", "mock"),
        )

    def _send(self, code: int, obj: dict, provider: str | None = "mock") -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if provider is not None:
            self.send_header("X-AI-Provider", provider)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def mock_ai():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAiHandler)
    server.mock_config = {
        "mode": "ok",
        "token": TEST_TOKEN,
        "requests": [],
        "conditions": DEFAULT_CONDITIONS,
        "provider": "mock",
    }
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def ai_client_app(db_path, mock_ai):
    """mock AI 서버를 가리키는 앱 클라이언트. (client, mock_ai_server)를 반환."""
    port = mock_ai.server_address[1]
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": db_path,
            "AI_INTERPRET_TIMEOUT_SECONDS": "25",
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        yield c, mock_ai
