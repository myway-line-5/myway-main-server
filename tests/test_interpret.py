"""해석 API 단위 테스트 — mock AI 스텁 사용 (AI 서버 불필요).

상세설계서 표 4-9/4-12 기준: 최초 current_conditions:null, exception 필수 boolean,
X-AI-Provider 모드 검증, 상태 코드 세부 분류, 수치 경계, 재시도 0회, 늦은 응답 폐기.
"""
import uuid

import pytest

from app.ai_client import AiInterpretResult
from app.config import Settings
from tests.conftest import DEFAULT_CONDITIONS, TEST_ENV_BASE, TEST_TOKEN

SID = str(uuid.uuid4())
HEADERS = {"X-Session-ID": SID}


def _post(client, message="강남에서 여의도까지 빨리"):
    return client.post("/v1/conditions/interpret", json={"user_message": message}, headers=HEADERS)


def _session(client, sid=SID):
    return client.app.state.session_store.get_or_create(sid)


def test_first_request_sends_current_conditions_null(ai_client_app):
    client, mock = ai_client_app
    r = _post(client)
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "success"
    assert body["ai_provider"] == "mock"  # X-AI-Provider 헤더 전달
    # 앱용 camelCase 변환 확인
    assert body["conditions"]["constraints"]["maxDurationMinutes"] == 40
    assert body["conditions"]["origin"]["rawName"] == "강남"
    # 메인→AI 요청: 계약 2필드만, 최초 current_conditions=null, Bearer 토큰
    sent = mock.mock_config["requests"][0]
    assert sent["path"] == "/v1/conditions/interpret"
    assert set(sent["body"].keys()) == {"current_conditions", "user_message"}
    assert sent["body"]["current_conditions"] is None  # 최초는 null (필드 유지)
    assert sent["authorization"] == f"Bearer {TEST_TOKEN}"
    # 성공 시 세션에 snake_case 원본 저장 + version/inflight 규칙
    s = _session(client)
    assert s.editing_condition == DEFAULT_CONDITIONS
    assert s.version == 1
    assert s.inflight == {}


def test_second_request_sends_full_current_conditions(ai_client_app):
    client, mock = ai_client_app
    _post(client)
    mock.mock_config["requests"].clear()
    _post(client, "환승 없이")
    sent = mock.mock_config["requests"][0]
    assert sent["body"]["current_conditions"] == DEFAULT_CONDITIONS  # snake_case 전체 조건


def test_business_failure_on_exception_true_with_200(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "business_error"
    mock.mock_config["error_code"] = "UNSUPPORTED_CONDITION"
    r = _post(client)
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "business_failure"
    assert body["error_code"] == "UNSUPPORTED_CONDITION"
    assert body["conditions"] is None
    s = _session(client)
    assert s.editing_condition is None  # 실패는 폐기 — 마지막 정상 조건 유지
    assert s.version == 0


def test_missing_exception_field_is_not_success(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "missing_exception"
    r = _post(client)
    body = r.json()
    assert body["result"] == "communication_failure"  # 비정상 응답 — 미반영
    assert body["error_code"] == "MALFORMED_RESPONSE"
    assert _session(client).editing_condition is None


def test_401_maps_to_auth_error(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "unauthorized"
    r = _post(client)
    assert r.json()["result"] == "auth_error"
    assert _session(client).editing_condition is None


def test_422_maps_to_business_failure(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "bad_request"
    body = _post(client).json()
    assert body["result"] == "business_failure"
    assert body["error_code"] == "INVALID_REQUEST"


def test_429_passes_body_error_code_through(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "rate_limited"
    body = _post(client).json()
    assert body["result"] == "communication_failure"
    # 표 4-12: 본문의 error_code가 있으면 그대로 전달 (없을 때만 AI_UNAVAILABLE 폴백)
    assert body["error_code"] == "RATE_LIMITED"
    assert _session(client).editing_condition is None


def test_500_maps_to_communication_failure(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "server_error"
    assert _post(client).json()["result"] == "communication_failure"


def test_provider_header_mismatch_not_applied(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["provider"] = "gemini"  # 기대 모드는 mock
    body = _post(client).json()
    assert body["result"] == "communication_failure"
    assert body["error_code"] == "AI_PROVIDER_MISMATCH"
    assert _session(client).editing_condition is None  # 미반영


def test_provider_header_missing_not_applied(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["provider"] = None  # 헤더 없음
    body = _post(client).json()
    assert body["result"] == "communication_failure"
    assert _session(client).editing_condition is None


def test_timeout_no_retry(ai_client_app, db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["mode"] = "sleep"
    mock_ai.mock_config["sleep_seconds"] = 3
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": db_path,
            "AI_INTERPRET_TIMEOUT_SECONDS": "1",
        }
    )
    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        r = _post(client)
    assert r.json()["result"] == "communication_failure"
    assert len(mock_ai.mock_config["requests"]) == 1  # 자동 재전송 0회


@pytest.mark.parametrize(
    "constraints",
    [
        {"max_duration_minutes": 1, "max_transfers": 0, "max_fare_krw": 0},
        {"max_duration_minutes": 1440, "max_transfers": 50, "max_fare_krw": 1_000_000},
    ],
)
def test_condition_range_boundaries_ok(ai_client_app, constraints):
    client, mock = ai_client_app
    mock.mock_config["conditions"] = {**DEFAULT_CONDITIONS, "constraints": constraints}
    assert _post(client).json()["result"] == "success"


def _full_constraints(**override):
    base = {"max_duration_minutes": None, "max_transfers": None, "max_fare_krw": None}
    base.update(override)
    return base


@pytest.mark.parametrize(
    "constraints",
    [
        _full_constraints(max_duration_minutes=0),
        _full_constraints(max_duration_minutes=1441),
        _full_constraints(max_transfers=-1),
        _full_constraints(max_transfers=51),
        _full_constraints(max_fare_krw=-1),
        _full_constraints(max_fare_krw=1_000_001),
        _full_constraints(max_fare_krw=1.5),  # 정수 아님
    ],
)
def test_condition_range_violations_rejected(ai_client_app, constraints):
    client, mock = ai_client_app
    mock.mock_config["conditions"] = {**DEFAULT_CONDITIONS, "constraints": constraints}
    body = _post(client).json()
    assert body["result"] == "business_failure"
    assert body["error_code"] == "INVALID_CONDITION_RANGE"
    assert _session(client).editing_condition is None  # 검증 실패도 폐기


@pytest.mark.parametrize("message", ["", "   ", "x" * 1001])
def test_invalid_user_message(ai_client_app, message):
    client, mock = ai_client_app
    body = _post(client, message).json()
    assert body["result"] == "business_failure"
    assert body["error_code"] == "INVALID_USER_MESSAGE"
    assert mock.mock_config["requests"] == []  # AI 호출 자체를 안 함


def test_stale_response_discarded(ai_client_app):
    """AI 응답 도착 전에 세션 version이 바뀌면(늦은 응답) 폐기하고 마지막 정상 조건 유지."""
    client, mock = ai_client_app
    store = client.app.state.session_store

    class BumpingAi:
        def interpret(self, current_conditions, user_message):
            store.confirm_station_slot(SID, "origin", "L2_MAIN_001", "시청")  # AI 처리 중 version 변경
            return AiInterpretResult(
                200,
                {"exception": False, "error_code": None, "conditions": {**DEFAULT_CONDITIONS}},
                "mock",
            )

    client.app.state.ai_client = BumpingAi()
    r = _post(client)
    assert r.json()["result"] == "communication_failure"
    assert r.json()["error_code"] == "STALE_RESPONSE_DISCARDED"
    s = _session(client)
    assert s.editing_condition["stations"]["origin"]["raw_name"] == "시청"  # confirm 결과는 유지
    assert s.version == 1


def test_settings_validation():
    with pytest.raises(RuntimeError):
        Settings(env={**TEST_ENV_BASE, "AI_SERVER_AUTH_TOKEN": ""})  # 토큰 누락
    with pytest.raises(RuntimeError):
        Settings(env={k: v for k, v in TEST_ENV_BASE.items() if k != "AI_SERVER_BASE_URL"})  # URL 누락
    with pytest.raises(RuntimeError):
        Settings(env={**TEST_ENV_BASE, "AI_SERVER_BASE_URL": "ftp://x"})  # http/https 아님
    with pytest.raises(RuntimeError):
        Settings(env={k: v for k, v in TEST_ENV_BASE.items() if k != "MYWAY_EXPECTED_AI_PROVIDER"})  # provider 누락
    with pytest.raises(RuntimeError):
        Settings(env={**TEST_ENV_BASE, "MYWAY_EXPECTED_AI_PROVIDER": "openai"})  # 목록 밖
    ok = Settings(env=TEST_ENV_BASE)
    assert ok.expected_ai_provider == "mock"
    assert ok.ai_interpret_timeout_seconds == 25.0
