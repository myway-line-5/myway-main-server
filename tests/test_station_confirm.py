"""역 확정/해제/교환 API 단위 테스트.

연동 8항목 중 6) 직접 선택(역 확정) 동기화, 7) 동명 역·교환·역 해제·제한 초기화 구분.
"""
import uuid

SID = str(uuid.uuid4())
HEADERS = {"X-Session-ID": SID}


def test_resolve_returns_candidates_for_raw_name(client):
    r = client.post("/v1/stations/resolve", json={"raw_name": "을지로3가"})
    assert r.status_code == 200
    body = r.json()
    assert body["raw_name"] == "을지로3가"  # 원문 보존
    assert body["suggested_name"] is None
    assert {s["line"] for s in body["candidates"]} == {"2", "3"}
    assert body["suggested_candidates"] == []


def test_resolve_keeps_suggestion_separate(client):
    r = client.post(
        "/v1/stations/resolve", json={"raw_name": "을지로3가아", "suggested_name": "을지로3가"}
    )
    body = r.json()
    assert body["raw_name"] == "을지로3가아"
    assert body["suggested_name"] == "을지로3가"
    assert body["candidates"] == []
    assert len(body["suggested_candidates"]) == 2


def test_resolve_same_group_candidates_not_merged(client):
    # 같은 group_id(동명) 후보는 그대로 모두 전달
    r = client.post("/v1/stations/resolve", json={"raw_name": "을지로3가"})
    assert len(r.json()["candidates"]) == 2


def test_resolve_works_without_session(client):
    r = client.post("/v1/stations/resolve", json={"raw_name": "시청"})
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1


def test_confirm_reflects_slot_and_bumps_version(client):
    r = client.post(
        "/v1/stations/confirm",
        json={"slot": "origin", "station_id": "L2_MAIN_001"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "success"
    assert body["station"]["station_id"] == "L2_MAIN_001"
    assert body["version"] == 1
    # 앱용 camelCase: rawName 갱신, suggestedName=null
    assert body["conditions"]["origin"] == {"rawName": "시청", "suggestedName": None}

    r2 = client.post(
        "/v1/stations/confirm",
        json={"slot": "destination", "station_id": "L5_539"},
        headers=HEADERS,
    )
    body2 = r2.json()
    assert body2["version"] == 2
    assert body2["conditions"]["destination"] == {"rawName": "여의도", "suggestedName": None}

    s = client.app.state.session_store.get_or_create(SID)
    assert s.confirmed_stations == {"origin": "L2_MAIN_001", "destination": "L5_539"}
    assert s.editing_condition["stations"]["origin"]["raw_name"] == "시청"


def test_confirm_does_not_call_ai(client, monkeypatch):
    class ExplodingAi:
        def interpret(self, *a, **k):
            raise AssertionError("역 확정은 AI 재호출이 없어야 한다")

    monkeypatch.setattr(client.app.state, "ai_client", ExplodingAi())
    r = client.post(
        "/v1/stations/confirm",
        json={"slot": "origin", "station_id": "L2_MAIN_001"},
        headers=HEADERS,
    )
    assert r.json()["result"] == "success"


def test_confirm_unknown_station(client):
    r = client.post(
        "/v1/stations/confirm",
        json={"slot": "origin", "station_id": "NOPE"},
        headers=HEADERS,
    )
    body = r.json()
    assert body["result"] == "business_failure"
    assert body["error_code"] == "STATION_NOT_FOUND"


def test_confirm_inactive_station(client):
    r = client.post(
        "/v1/stations/confirm",
        json={"slot": "origin", "station_id": "L9_TEST_999"},
        headers=HEADERS,
    )
    body = r.json()
    assert body["result"] == "business_failure"
    assert body["error_code"] == "STATION_INACTIVE"
    assert client.app.state.session_store.get_or_create(SID).version == 0


def test_release_station(client):
    client.post(
        "/v1/stations/confirm",
        json={"slot": "origin", "station_id": "L2_MAIN_001"},
        headers=HEADERS,
    )
    r = client.post("/v1/stations/release", json={"slot": "origin"}, headers=HEADERS)
    body = r.json()
    assert body["result"] == "success"
    assert body["version"] == 2
    assert body["conditions"]["origin"] is None  # 해당 슬롯 해제
    s = client.app.state.session_store.get_or_create(SID)
    assert s.confirmed_stations == {}  # 확정 ID도 해제


def test_swap_stations(client):
    client.post(
        "/v1/stations/confirm", json={"slot": "origin", "station_id": "L2_MAIN_001"}, headers=HEADERS
    )
    client.post(
        "/v1/stations/confirm", json={"slot": "destination", "station_id": "L5_539"}, headers=HEADERS
    )
    r = client.post("/v1/stations/swap", headers=HEADERS)
    body = r.json()
    assert body["result"] == "success"
    assert body["version"] == 3
    # 슬롯과 ID 대응이 함께 이동
    assert body["conditions"]["origin"]["rawName"] == "여의도"
    assert body["conditions"]["destination"]["rawName"] == "시청"
    s = client.app.state.session_store.get_or_create(SID)
    assert s.confirmed_stations == {"origin": "L5_539", "destination": "L2_MAIN_001"}
