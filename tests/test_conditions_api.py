"""조건 변경 계열 API 테스트 — reset(조건 모두 초기화) / new-search(새 검색).

상세설계서 4.2.3: "조건 모두 초기화"는 제한·선호만 해제하고 역은 유지 / "새 검색"은
현재 조건·확정 역·결과를 모두 초기화하고 진행 중 해석을 무효화한다.
"""
import uuid

SID = str(uuid.uuid4())
HEADERS = {"X-Session-ID": SID}


def _confirm(client, slot, station_id):
    return client.post(
        "/v1/stations/confirm",
        json={"slot": slot, "station_id": station_id},
        headers=HEADERS,
    )


def test_reset_keeps_confirmed_stations(client):
    _confirm(client, "origin", "L2_MAIN_001")
    r = client.post("/v1/conditions/reset", headers=HEADERS)
    body = r.json()
    assert body["result"] == "success"
    assert body["version"] == 2
    # 역은 유지 — editing_condition을 None으로 만들지 않는다 (4.2.3: 초기화≠새 검색)
    cond = body["conditions"]
    assert cond["origin"] == {"rawName": "시청", "suggestedName": None}
    assert cond["constraints"] == {"maxDurationMinutes": None, "maxTransfers": None, "maxFareKrw": None}
    assert cond["preference"] is None
    s = client.app.state.session_store.get_or_create(SID)
    assert s.confirmed_stations == {"origin": "L2_MAIN_001"}  # 역은 유지


def test_new_search_clears_stations_and_conditions(client):
    _confirm(client, "origin", "L2_MAIN_001")
    _confirm(client, "destination", "L5_539")
    r = client.post("/v1/conditions/new-search", headers=HEADERS)
    body = r.json()
    assert body["result"] == "success"
    assert body["version"] == 3
    assert body["conditions"] is None
    s = client.app.state.session_store.get_or_create(SID)
    assert s.editing_condition is None  # 다음 AI 요청은 current_conditions: null
    assert s.confirmed_stations == {}


def test_reset_then_interpret_sends_null(ai_client_app):
    """새 검색 후 다음 interpret 요청은 current_conditions: null이어야 한다."""
    client, mock = ai_client_app
    r = client.post("/v1/conditions/interpret", json={"user_message": "강남에서 여의도"}, headers=HEADERS)
    assert r.json()["result"] == "success"
    client.post("/v1/conditions/new-search", headers=HEADERS)
    mock.mock_config["requests"].clear()
    r2 = client.post(
        "/v1/conditions/interpret", json={"user_message": "처음부터 다시"}, headers=HEADERS
    )
    assert r2.json()["result"] == "success"
    assert mock.mock_config["requests"][0]["body"]["current_conditions"] is None
