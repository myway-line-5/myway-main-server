"""실시간 API 라우터·서울 클라이언트 시험.

mock 서울시 서버(ThreadingHTTPServer)로 도착정보·위치정보 계약을 검증한다.
fixture는 2026-11-28 실측 응답(강남 도착, 9호선 위치)을 축약한 것이다.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from tests.conftest import TEST_ENV_BASE

# ── 실측 기반 fixture ──────────────────────────────────────────────────────
# 강남 도착 실측: 2호선 외선(140초) + 2호선 내선(전역 도착) + 신분당선 2행.
# 라우터는 subwayId로 2/5/9만 전달해야 한다 (환승 노선 행은 버림).
ARRIVAL_BODY = {
    "errorMessage": {
        "status": 200,
        "code": "INFO-000",
        "message": "정상 처리되었습니다.",
        "total": 7,
    },
    "realtimeArrivalList": [
        {
            "subwayId": "1002",
            "updnLine": "외선",
            "trainLineNm": "성수행 - 역삼방면",
            "statnNm": "강남",
            "btrainNo": "2227",
            "bstatnNm": "성수",
            "barvlDt": "140",
            "arvlMsg2": "2분 20초 후",
            "arvlMsg3": "서초",
            "arvlCd": "99",
            "btrainSttus": "일반",
            "lstcarAt": "0",
            "recptnDt": "2026-11-28 15:37:21",
        },
        {
            "subwayId": "1077",  # 신분당선 — 서비스 대상 아님, 필터 대상
            "updnLine": "상행",
            "trainLineNm": "신사행 - 신논현방면",
            "statnNm": "강남",
            "btrainNo": "19",
            "bstatnNm": "신사",
            "barvlDt": "0",
            "arvlMsg2": "[2]번째 전역 (양재시민의숲)",
            "arvlCd": "99",
            "btrainSttus": "일반",
            "lstcarAt": "0",
            "recptnDt": "2026-11-28 15:42:07",
        },
        {
            "subwayId": "1002",
            "updnLine": "내선",
            "trainLineNm": "성수행 - 교대방면",
            "statnNm": "강남",
            "btrainNo": "2256",
            "bstatnNm": "성수",
            "barvlDt": "60",
            "arvlMsg2": "전역 도착",
            "arvlMsg3": "역삼",
            "arvlCd": "5",
            "btrainSttus": "일반",
            "lstcarAt": "0",
            "recptnDt": "2026-11-28 15:37:21",
        },
    ],
}

# 9호선 위치 실측 축약: trainSttus 1=도착, 0=진입. updnLine '0'/'1'.
POSITION_BODY = {
    "errorMessage": {
        "status": 200,
        "code": "INFO-000",
        "message": "정상 처리되었습니다.",
        "total": 2,
    },
    "realtimePositionList": [
        {
            "subwayId": "1009",
            "statnId": "1009000925",
            "statnNm": "가양",
            "trainNo": "9103",
            "statnTnm": "개화",
            "updnLine": "0",
            "trainSttus": "1",
            "directAt": "0",
            "recptnDt": "2026-11-28 15:41:30",
        },
        {
            "subwayId": "1009",
            "statnId": "1009000930",
            "statnNm": "염창",
            "trainNo": "9597",
            "statnTnm": "중앙보훈병원",
            "updnLine": "1",
            "trainSttus": "0",
            "directAt": "1",
            "recptnDt": "2026-11-28 15:41:30",
        },
    ],
}

# 서울시 오류는 평탄한 형태로 온다 (실측 ERROR-336).
API_ERROR_BODY = {
    "status": 500,
    "code": "ERROR-336",
    "message": "빠른 시간내에 조회 부탁드립니다. 제한된 호출횟수를 초과하였습니다.",
}


class MockSeoulHandler(BaseHTTPRequestHandler):
    """서울시 실시간 API 대신 쓰는 mock 스텁 (GET만 처리).

    mode: ok | api_error | http_error | invalid_json | timeout
    """

    def do_GET(self) -> None:
        cfg = self.server.mock_config
        cfg["requests"].append(self.path)
        mode = cfg["mode"]
        if mode == "timeout":
            time.sleep(cfg.get("sleep_seconds", 1.5))
            self._send_json(200, ARRIVAL_BODY)
            return
        if mode == "http_error":
            self._send_json(500, {"status": 500, "code": "ERROR-500"})
            return
        if mode == "api_error":
            self._send_json(200, API_ERROR_BODY)
            return
        if mode == "invalid_json":
            data = b"<html>gateway error</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # ok: 경로의 서비스명으로 도착/위치 fixture를 고른다
        if "realtimeStationArrival" in self.path:
            self._send_json(200, ARRIVAL_BODY)
        elif "realtimePosition" in self.path:
            self._send_json(200, POSITION_BODY)
        else:
            self._send_json(404, {"error": "not found"})

    def _send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def mock_seoul():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockSeoulHandler)
    server.mock_config = {"mode": "ok", "requests": [], "sleep_seconds": 1.5}
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def seoul_app(db_path, mock_seoul):
    """SEOUL_API_KEY가 설정된 앱 클라이언트. (TestClient, mock_seoul) 반환."""
    port = mock_seoul.server_address[1]
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "MYWAY_DB_PATH": db_path,
            "SEOUL_API_KEY": "test-seoul-key",
            "SEOUL_API_BASE_URL": f"http://127.0.0.1:{port}",
            "SEOUL_API_TIMEOUT_SECONDS": "0.5",
            "SEOUL_REALTIME_CACHE_TTL_SECONDS": "30",
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        yield c, mock_seoul


# ── 도착정보 ────────────────────────────────────────────────────────────────


def test_arrivals_not_configured(client: TestClient):
    """키 미설정 시 not_configured — 다른 엔드포인트는 영향 없음."""
    r = client.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_configured"
    assert body["error_code"] == "SEOUL_API_NOT_CONFIGURED"
    assert body["items"] == []


def test_arrivals_ok_filters_unsupported_lines(seoul_app):
    """환승역은 타 노선 행도 온다 — 2/5/9만 전달되는지 (실측: 강남에 신분당선 포함)."""
    c, mock = seoul_app
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["station"]["station_id"] == "L5_539"
    assert len(body["items"]) == 2  # 신분당선 행은 제외
    first = body["items"][0]
    assert first["train_no"] == "2227"
    assert first["line"] == "2"
    assert first["direction"] == "down"  # 외선
    assert first["direction_label"] == "외선"
    assert first["destination"] == "성수"
    assert first["arrival_seconds"] == 140
    assert first["arrival_message"] == "2분 20초 후"
    assert first["arrival_code"] == "99"
    assert first["train_kind"] == "일반"
    assert first["is_last_train"] is False
    assert first["received_at"] == "2026-11-28 15:37:21"
    assert body["received_at"] == "2026-11-28 15:37:21"
    # 내선 → up 정규화
    assert body["items"][1]["direction"] == "up"
    assert body["items"][1]["arrival_code"] == "5"  # 전역 도착


def test_arrivals_line_filter(seoul_app):
    """line 파라미터로 노선 필터."""
    c, _ = seoul_app
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539", "line": "2"})
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["items"]) == 2
    assert all(i["line"] == "2" for i in body["items"])


def test_arrivals_station_not_found(seoul_app):
    c, _ = seoul_app
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L2_NOPE_000"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["error_code"] == "STATION_NOT_FOUND"


def test_arrivals_unsupported_line_param(seoul_app):
    c, _ = seoul_app
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539", "line": "7"})
    body = r.json()
    assert body["status"] == "error"
    assert body["error_code"] == "UNSUPPORTED_LINE"


def test_arrivals_seoul_api_error(seoul_app):
    """서울시 오류(평탄한 ERROR-336 형태) → unavailable, 값 추측 없음."""
    c, mock = seoul_app
    mock.mock_config["mode"] = "api_error"
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["error_code"] == "ERROR-336"
    assert body["items"] == []
    assert body["received_at"] is None
    # unavailable이어도 역 정보는 내려준다 (앱 화면 유지용)
    assert body["station"]["station_id"] == "L5_539"


def test_arrivals_invalid_json(seoul_app):
    c, mock = seoul_app
    mock.mock_config["mode"] = "invalid_json"
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["error_code"] == "SEOUL_INVALID_RESPONSE"


def test_arrivals_http_error(seoul_app):
    c, mock = seoul_app
    mock.mock_config["mode"] = "http_error"
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["error_code"] == "SEOUL_HTTP_500"


def test_arrivals_timeout(seoul_app):
    """타임아웃은 SEOUL_TRANSPORT_ERROR — 추정 모드 전환 근거."""
    c, mock = seoul_app
    mock.mock_config["mode"] = "timeout"
    r = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["error_code"] == "SEOUL_TRANSPORT_ERROR"


def test_arrivals_cached_within_ttl(seoul_app):
    """같은 역 재조회는 TTL 안에서 서버를 다시 치지 않는다 (호출 한도 관리)."""
    c, mock = seoul_app
    r1 = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    r2 = c.get("/v1/realtime/arrivals", params={"station_id": "L5_539"})
    assert r1.json()["status"] == "ok"
    assert r2.json()["status"] == "ok"
    arrival_requests = [p for p in mock.mock_config["requests"] if "realtimeStationArrival" in p]
    assert len(arrival_requests) == 1


# ── 열차 위치 ────────────────────────────────────────────────────────────────


def test_position_ok(seoul_app):
    """9호선 실측 행: 9103 가양 도착(1), 9597 염창 진입(0)+급행."""
    c, _ = seoul_app
    r = c.get("/v1/realtime/trains/9103", params={"line": "9"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    train = body["train"]
    assert train["train_no"] == "9103"
    assert train["line"] == "9"
    assert train["station_name"] == "가양"
    assert train["station_seoul_id"] == "1009000925"
    assert train["direction"] == "up"  # updnLine '0'
    assert train["status"] == "arrived"  # trainSttus '1'
    assert train["terminal"] == "개화"
    assert train["is_express"] is False
    assert body["received_at"] == "2026-11-28 15:41:30"


def test_position_express_and_approaching(seoul_app):
    c, _ = seoul_app
    body = c.get("/v1/realtime/trains/9597", params={"line": "9"}).json()
    assert body["status"] == "ok"
    train = body["train"]
    assert train["status"] == "approaching"  # trainSttus '0'
    assert train["direction"] == "down"  # updnLine '1'
    assert train["is_express"] is True


def test_position_train_not_found(seoul_app):
    """호선 응답에 없는 열차번호 → TRAIN_NOT_FOUND (미운행·고정 실패 근거)."""
    c, _ = seoul_app
    body = c.get("/v1/realtime/trains/0000", params={"line": "9"}).json()
    assert body["status"] == "unavailable"
    assert body["error_code"] == "TRAIN_NOT_FOUND"
    assert body["train"] is None


def test_position_unsupported_line(seoul_app):
    c, _ = seoul_app
    body = c.get("/v1/realtime/trains/9103", params={"line": "7"}).json()
    assert body["status"] == "error"
    assert body["error_code"] == "UNSUPPORTED_LINE"


def test_position_not_configured(client: TestClient):
    body = client.get("/v1/realtime/trains/9103", params={"line": "9"}).json()
    assert body["status"] == "not_configured"


def test_position_seoul_api_error(seoul_app):
    c, mock = seoul_app
    mock.mock_config["mode"] = "api_error"
    body = c.get("/v1/realtime/trains/9103", params={"line": "9"}).json()
    assert body["status"] == "unavailable"
    assert body["error_code"] == "ERROR-336"


def test_position_cached_within_ttl(seoul_app):
    c, mock = seoul_app
    b1 = c.get("/v1/realtime/trains/9103", params={"line": "9"}).json()
    b2 = c.get("/v1/realtime/trains/9597", params={"line": "9"}).json()
    assert b1["status"] == "ok" and b2["status"] == "ok"
    position_requests = [p for p in mock.mock_config["requests"] if "realtimePosition" in p]
    assert len(position_requests) == 1
