"""초기화 계열 엔드포인트 통합 테스트 — 상세설계서 4.2.3 / 표 4-12 구분.

- "조건 모두 초기화"(reset)는 역을 유지하고 제한·선호만 해제 → 다음 AI 요청의
  current_conditions는 null이 아니라 stations를 포함한 객체다.
- "새 검색"(new-search)은 조건·확정 역을 모두 초기화 → 다음 AI 요청은 current_conditions: null.
- 503 + error_code 본문이 있으면 그대로 전달 (SERVICE_NOT_CONFIGURED 설정 오류 구분),
  없으면 AI_UNAVAILABLE 폴백.
"""
import uuid

from tests.conftest import DEFAULT_CONDITIONS

SID = str(uuid.uuid4())
HEADERS = {"X-Session-ID": SID}

FILLED_CONDITIONS = {
    "stations": {
        "origin": {"raw_name": "시청", "suggested_name": None},
        "destination": {"raw_name": "여의도", "suggested_name": None},
    },
    "constraints": {"max_duration_minutes": 40, "max_transfers": 1, "max_fare_krw": 5000},
    "preference": "fastest",
}


def _post(client, message="강남에서 여의도까지 빨리"):
    return client.post("/v1/conditions/interpret", json={"user_message": message}, headers=HEADERS)


def _confirm_both(client):
    client.post(
        "/v1/stations/confirm", json={"slot": "origin", "station_id": "L2_MAIN_001"}, headers=HEADERS
    )
    client.post(
        "/v1/stations/confirm", json={"slot": "destination", "station_id": "L5_539"}, headers=HEADERS
    )


def test_after_reset_current_conditions_keeps_stations_but_null_constraints(ai_client_app):
    client, mock = ai_client_app
    _confirm_both(client)
    # AI 해석 성공으로 제한·선호가 채워진 상태를 만든다
    mock.mock_config["conditions"] = FILLED_CONDITIONS
    _post(client)

    r = client.post("/v1/conditions/reset", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "success"
    # 앱 응답: 역은 유지, 제한·선호는 null (키 유지)
    cond = body["conditions"]
    assert cond["origin"] == {"rawName": "시청", "suggestedName": None}
    assert cond["destination"] == {"rawName": "여의도", "suggestedName": None}
    assert cond["constraints"] == {"maxDurationMinutes": None, "maxTransfers": None, "maxFareKrw": None}
    assert cond["preference"] is None

    # 다음 해석 요청: current_conditions가 null이 아니고 stations를 포함, constraints는 전부 null
    mock.mock_config["requests"].clear()
    mock.mock_config["conditions"] = DEFAULT_CONDITIONS
    _post(client)
    sent = mock.mock_config["requests"][0]["body"]["current_conditions"]
    assert sent is not None
    assert sent["stations"]["origin"]["raw_name"] == "시청"
    assert sent["stations"]["destination"]["raw_name"] == "여의도"
    assert sent["constraints"] == {"max_duration_minutes": None, "max_transfers": None, "max_fare_krw": None}
    assert sent["preference"] is None

    # 세션의 확정 역 ID도 유지
    s = client.app.state.session_store.get_or_create(SID)
    assert s.confirmed_stations == {"origin": "L2_MAIN_001", "destination": "L5_539"}


def test_after_new_search_current_conditions_is_null(ai_client_app):
    client, mock = ai_client_app
    _confirm_both(client)
    mock.mock_config["conditions"] = FILLED_CONDITIONS
    _post(client)

    r = client.post("/v1/conditions/new-search", headers=HEADERS)
    assert r.json()["result"] == "success"
    assert r.json()["conditions"] is None

    mock.mock_config["requests"].clear()
    mock.mock_config["conditions"] = DEFAULT_CONDITIONS
    _post(client)
    # 대조 케이스: 새 검색 후에는 current_conditions가 null이다
    assert mock.mock_config["requests"][0]["body"]["current_conditions"] is None
    s = client.app.state.session_store.get_or_create(SID)
    assert s.confirmed_stations == {}


def test_503_with_error_code_is_passed_through(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "service_unavailable"
    mock.mock_config["error_code"] = "SERVICE_NOT_CONFIGURED"
    body = _post(client).json()
    assert body["result"] == "communication_failure"
    assert body["error_code"] == "SERVICE_NOT_CONFIGURED"  # 표 4-12: 설정 오류로 구분 전달


def test_503_without_error_code_falls_back_to_ai_unavailable(ai_client_app):
    client, mock = ai_client_app
    mock.mock_config["mode"] = "service_unavailable"
    mock.mock_config.pop("error_code", None)
    body = _post(client).json()
    assert body["result"] == "communication_failure"
    assert body["error_code"] == "AI_UNAVAILABLE"
