"""역 목록 API 단위 테스트.

fixture는 앱 Fake와 같은 케이스 기준: 동명 다른 노선 / 비활성 역 / 존재하지 않는 이름.
"""


def test_list_all_stations_includes_inactive(client):
    r = client.get("/v1/stations")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 6  # 비활성 역도 is_active 플래그와 함께 전체에 포함된다
    assert all(set(s.keys()) == {"station_id", "name", "line", "is_active"} for s in items)


def test_station_dto_line_is_raw_string(client):
    r = client.get("/v1/stations")
    lines = {s["line"] for s in r.json()["items"]}
    assert lines == {"2", "3", "5", "9"}  # "호선" 접미사 없이 원본 문자열 그대로


def test_search_duplicate_name_returns_all_lines(client):
    r = client.get("/v1/stations/search", params={"q": "을지로3가"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert {s["line"] for s in items} == {"2", "3"}  # 동명 다른 노선을 line과 함께 모두 반환


def test_search_yeouido_two_lines(client):
    r = client.get("/v1/stations/search", params={"q": "여의도"})
    items = r.json()["items"]
    assert {s["line"] for s in items} == {"5", "9"}


def test_search_no_result_returns_empty_list_not_error(client):
    r = client.get("/v1/stations/search", params={"q": "존재하지않는역"})
    assert r.status_code == 200  # 오류가 아니라 빈 리스트
    assert r.json() == {"items": []}


def test_search_trims_query(client):
    r = client.get("/v1/stations/search", params={"q": "  시청  "})
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["items"]] == ["시청"]


def test_search_empty_query_returns_all(client):
    r = client.get("/v1/stations/search", params={"q": ""})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 6


def test_search_inactive_station_flagged(client):
    r = client.get("/v1/stations/search", params={"q": "시험용비활성역"})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["is_active"] is False


def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/v1/stations" in spec["paths"]
    assert "/v1/stations/search" in spec["paths"]
