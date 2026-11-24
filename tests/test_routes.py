import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Database, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.conftest import TEST_ENV_BASE  # noqa: E402

ROUTE_STATIONS = [
    ("T_A", "g_a", "테스트A", "2", 1),
    ("T_B", "g_b", "테스트B", "2", 1),
    ("T_C", "g_c", "테스트C", "2", 1),
    ("T_D", "g_d", "테스트D", "5", 1),
    ("T_E", "g_e", "테스트E", "2", 0),
    ("T_F", "g_f", "고립역", "2", 1),
]

ROUTE_EDGES = [
    ("E1", "T_A", "T_B", 60, 500, 0),
    ("E2", "T_B", "T_A", 60, 500, 0),
    ("E3", "T_B", "T_C", 60, 500, 0),
    ("E4", "T_C", "T_B", 60, 500, 0),
    ("E5", "T_C", "T_D", 120, 300, 1),
    ("E6", "T_D", "T_C", 120, 300, 1),
]


@pytest.fixture
def route_db_path(tmp_path) -> str:
    path = str(tmp_path / "route.db")
    init_db(path)
    db = Database(path)
    for row in ROUTE_STATIONS:
        db.execute(
            "INSERT INTO Station (station_id, group_id, name, line, is_active) VALUES (?, ?, ?, ?, ?)",
            row,
        )
    for row in ROUTE_EDGES:
        db.execute(
            "INSERT INTO RouteEdge (edge_id, from_station_id, to_station_id, seconds, meters, is_transfer) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            row,
        )
    db.close()
    return path


@pytest.fixture
def route_client(route_db_path) -> TestClient:
    settings = Settings(env={**TEST_ENV_BASE, "MYWAY_DB_PATH": route_db_path})
    application = create_app(settings)
    with TestClient(application) as c:
        yield c


def _session_headers(client: TestClient) -> dict[str, str]:
    resp = client.post("/v1/conditions/new-search", headers={"X-Session-ID": "11111111-1111-4111-8111-111111111111"})
    sid = resp.headers["X-Session-ID"]
    return {"X-Session-ID": sid}


def test_search_ok_with_explicit_ids(route_client):
    headers = _session_headers(route_client)
    resp = route_client.post(
        "/v1/routes/search",
        json={"origin_station_id": "T_A", "destination_station_id": "T_D"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    route = body["route"]
    assert route["type"] == "SHORTEST_TIME"
    assert len(route["segments"]) == 3
    assert route["total_seconds"] == 240
    assert route["total_meters"] == 1300
    assert route["transfer_count"] == 1
    assert route["segments"][2]["is_transfer"] is True
    assert route["segments"][0]["line_name"] == "2"
    assert route["total_fare_krw"] == 1550
    assert route["fare_version"] is None
    assert body["metrics"]["elapsed_ms"] < 1000


def test_search_uses_confirmed_stations(route_client):
    headers = _session_headers(route_client)
    route_client.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
    route_client.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
    resp = route_client.post("/v1/routes/search", json={}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_search_body_ids_override_confirmed(route_client):
    headers = _session_headers(route_client)
    route_client.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
    route_client.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
    resp = route_client.post(
        "/v1/routes/search",
        json={"origin_station_id": "T_D", "destination_station_id": "T_A"},
        headers=headers,
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["route"]["segments"][0]["from_station"]["station_id"] == "T_D"


def test_search_origin_not_confirmed(route_client):
    headers = _session_headers(route_client)
    route_client.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
    resp = route_client.post("/v1/routes/search", json={}, headers=headers)
    assert resp.json()["status"] == "error"
    assert resp.json()["error_code"] == "ORIGIN_NOT_CONFIRMED"


def test_search_destination_not_confirmed(route_client):
    headers = _session_headers(route_client)
    resp = route_client.post("/v1/routes/search", json={"origin_station_id": "T_A"}, headers=headers)
    assert resp.json()["status"] == "error"
    assert resp.json()["error_code"] == "DESTINATION_NOT_CONFIRMED"


def test_search_station_not_found(route_client):
    headers = _session_headers(route_client)
    resp = route_client.post(
        "/v1/routes/search",
        json={"origin_station_id": "T_A", "destination_station_id": "NOPE"},
        headers=headers,
    )
    assert resp.json()["status"] == "error"
    assert resp.json()["error_code"] == "STATION_NOT_FOUND"


def test_search_station_inactive(route_client):
    headers = _session_headers(route_client)
    resp = route_client.post(
        "/v1/routes/search",
        json={"origin_station_id": "T_A", "destination_station_id": "T_E"},
        headers=headers,
    )
    assert resp.json()["status"] == "error"
    assert resp.json()["error_code"] == "STATION_INACTIVE"


def test_search_same_station(route_client):
    headers = _session_headers(route_client)
    resp = route_client.post(
        "/v1/routes/search",
        json={"origin_station_id": "T_A", "destination_station_id": "T_A"},
        headers=headers,
    )
    assert resp.json()["status"] == "error"
    assert resp.json()["error_code"] == "SAME_STATION"


def test_search_no_route_isolated(route_client):
    headers = _session_headers(route_client)
    resp = route_client.post(
        "/v1/routes/search",
        json={"origin_station_id": "T_F", "destination_station_id": "T_A"},
        headers=headers,
    )
    assert resp.json()["status"] == "no_route"
    assert resp.json()["route"] is None


def test_search_confirms_conditions_and_bumps_version(route_client):
    headers = _session_headers(route_client)
    app = route_client.app
    store = app.state.session_store
    sid = headers["X-Session-ID"]
    before = store.get_or_create(sid).version
    route_client.post(
        "/v1/routes/search",
        json={"origin_station_id": "T_A", "destination_station_id": "T_D"},
        headers=headers,
    )
    after = store.get_or_create(sid).version
    assert after == before + 1
    assert store.get_or_create(sid).confirmed_condition is None


def test_search_with_constraints_via_interpret(route_db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["conditions"] = {
        "stations": {
            "origin": {"raw_name": "테스트A", "suggested_name": None},
            "destination": {"raw_name": "테스트D", "suggested_name": None},
        },
        "constraints": {"max_duration_minutes": 1, "max_transfers": None, "max_fare_krw": None},
        "preference": None,
    }
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": route_db_path,
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        headers = _session_headers(c)
        c.post("/v1/conditions/interpret", json={"user_message": "테스트A에서 테스트D까지"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
        resp = c.post("/v1/routes/search", json={}, headers=headers)
        assert resp.json()["status"] == "no_route"


def test_search_constraints_satisfied_ok(route_db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["conditions"] = {
        "stations": {
            "origin": {"raw_name": "테스트A", "suggested_name": None},
            "destination": {"raw_name": "테스트D", "suggested_name": None},
        },
        "constraints": {"max_duration_minutes": 40, "max_transfers": 2, "max_fare_krw": None},
        "preference": None,
    }
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": route_db_path,
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        headers = _session_headers(c)
        c.post("/v1/conditions/interpret", json={"user_message": "테스트A에서 테스트D까지 40분 안에"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
        resp = c.post("/v1/routes/search", json={}, headers=headers)
        body = resp.json()
        assert body["status"] == "ok"
        assert body["route"]["total_seconds"] == 240


def test_search_minimum_transfer_preference(route_db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["conditions"] = {
        "stations": {
            "origin": {"raw_name": "테스트A", "suggested_name": None},
            "destination": {"raw_name": "테스트D", "suggested_name": None},
        },
        "constraints": {"max_duration_minutes": None, "max_transfers": None, "max_fare_krw": None},
        "preference": "minimum_transfer",
    }
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": route_db_path,
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        headers = _session_headers(c)
        c.post("/v1/conditions/interpret", json={"user_message": "환승 적게"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
        resp = c.post("/v1/routes/search", json={}, headers=headers)
        body = resp.json()
        assert body["status"] == "ok"
        assert body["route"]["type"] == "MIN_TRANSFERS"


def test_search_min_transfer_with_constraints_rejected(route_db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["conditions"] = {
        "stations": {
            "origin": {"raw_name": "테스트A", "suggested_name": None},
            "destination": {"raw_name": "테스트D", "suggested_name": None},
        },
        "constraints": {"max_duration_minutes": 40, "max_transfers": None, "max_fare_krw": None},
        "preference": "minimum_transfer",
    }
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": route_db_path,
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        headers = _session_headers(c)
        c.post("/v1/conditions/interpret", json={"user_message": "환승 적게 40분 안에"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
        resp = c.post("/v1/routes/search", json={}, headers=headers)
        assert resp.json()["status"] == "error"
        assert resp.json()["error_code"] == "CONSTRAINTS_NOT_SUPPORTED"


def test_search_fare_preference_ok(route_db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["conditions"] = {
        "stations": {
            "origin": {"raw_name": "테스트A", "suggested_name": None},
            "destination": {"raw_name": "테스트D", "suggested_name": None},
        },
        "constraints": {"max_duration_minutes": None, "max_transfers": None, "max_fare_krw": None},
        "preference": "min_fare",
    }
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": route_db_path,
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        headers = _session_headers(c)
        c.post("/v1/conditions/interpret", json={"user_message": "요금 적게"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
        resp = c.post("/v1/routes/search", json={}, headers=headers)
        body = resp.json()
        assert body["status"] == "ok"
        assert body["route"]["type"] == "MIN_FARE"
        assert body["route"]["total_fare_krw"] == 1550


def test_search_max_fare_constraint_exceeded(route_db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["conditions"] = {
        "stations": {
            "origin": {"raw_name": "테스트A", "suggested_name": None},
            "destination": {"raw_name": "테스트D", "suggested_name": None},
        },
        "constraints": {"max_duration_minutes": None, "max_transfers": None, "max_fare_krw": 1000},
        "preference": None,
    }
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": route_db_path,
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        headers = _session_headers(c)
        c.post("/v1/conditions/interpret", json={"user_message": "테스트A에서 테스트D까지 1000원 안에"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
        resp = c.post("/v1/routes/search", json={}, headers=headers)
        assert resp.json()["status"] == "no_route"


def test_search_max_fare_constraint_satisfied(route_db_path, mock_ai):
    port = mock_ai.server_address[1]
    mock_ai.mock_config["conditions"] = {
        "stations": {
            "origin": {"raw_name": "테스트A", "suggested_name": None},
            "destination": {"raw_name": "테스트D", "suggested_name": None},
        },
        "constraints": {"max_duration_minutes": None, "max_transfers": None, "max_fare_krw": 2000},
        "preference": None,
    }
    settings = Settings(
        env={
            **TEST_ENV_BASE,
            "AI_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
            "MYWAY_DB_PATH": route_db_path,
        }
    )
    application = create_app(settings)
    with TestClient(application) as c:
        headers = _session_headers(c)
        c.post("/v1/conditions/interpret", json={"user_message": "테스트A에서 테스트D까지 2000원 안에"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "origin", "station_id": "T_A"}, headers=headers)
        c.post("/v1/stations/confirm", json={"slot": "destination", "station_id": "T_D"}, headers=headers)
        resp = c.post("/v1/routes/search", json={}, headers=headers)
        body = resp.json()
        assert body["status"] == "ok"
        assert body["route"]["total_fare_krw"] == 1550
