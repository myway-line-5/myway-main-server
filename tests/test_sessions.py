"""세션/조건 상태 관리 단위 테스트.

연동 8항목 중 해당: 직접 선택(역 확정) 동기화, 늦은 응답 폐기, 동시 세션 독립,
동명·교환·역 해제·제한 초기화 구분.
"""
import uuid

import pytest
from fastapi import Depends

from app.config import Settings
from app.sessions import SessionStore, get_session
from tests.conftest import TEST_ENV_BASE


def test_startup_fails_without_token():
    with pytest.raises(RuntimeError):
        Settings(env={})


def test_two_sessions_are_isolated():
    store = SessionStore()
    a = store.get_or_create(str(uuid.uuid4()))
    b = store.get_or_create(str(uuid.uuid4()))
    store.begin_request(a.session_id, "r1")
    assert store.apply_ai_response(a.session_id, "r1", {"preference": None})
    assert a.editing_condition == {"preference": None}
    assert b.editing_condition is None
    assert a.version == 1 and b.version == 0


def test_ai_response_applied_when_version_matches():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    base = store.begin_request(s.session_id, "req-1")
    assert base == 0
    ok = store.apply_ai_response(s.session_id, "req-1", {"max_transfers": 2})
    assert ok is True
    assert s.editing_condition == {"max_transfers": 2}
    assert s.version == 1
    assert s.inflight == {}


def test_late_response_is_discarded():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.begin_request(s.session_id, "req-1")  # 기반 version 0
    assert store.apply_ai_response(s.session_id, "req-1", {"max_fare_krw": 2000}) is True
    assert s.version == 1
    # 진행 중 version이 바뀐 요청의 늦은 응답은 폐기되고 마지막 정상 조건이 유지된다
    store.begin_request(s.session_id, "req-old")  # 기반 version 1
    store.confirm_conditions(s.session_id)  # version 2로 변경
    assert store.apply_ai_response(s.session_id, "req-old", {"max_fare_krw": 9999}) is False
    assert s.editing_condition == {"max_fare_krw": 2000}
    assert s.version == 2


def test_begin_request_blocked_while_another_inflight():
    """같은 세션에 진행 중인 요청이 있으면 새 begin_request는 None (AI 호출 생략)."""
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    assert store.begin_request(s.session_id, "r1") == 0
    assert store.begin_request(s.session_id, "r2") is None  # 진행 중 — 기록 안 함
    assert set(s.inflight) == {"r1"}
    store.abort_request(s.session_id, "r1")
    assert store.begin_request(s.session_id, "r2") == 0  # 해제 후에는 가능
    assert set(s.inflight) == {"r2"}


def test_unknown_request_id_is_discarded():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    assert store.apply_ai_response(s.session_id, "no-such-request", {}) is False
    assert s.editing_condition is None
    assert s.version == 0


def test_confirm_station_slot_updates_condition_and_bumps_version():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    version = store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_001", "시청")
    assert version == 1
    # 슬롯은 {raw_name: 선택 역명, suggested_name: null}로 갱신
    assert s.editing_condition["stations"]["origin"] == {"raw_name": "시청", "suggested_name": None}
    assert s.confirmed_stations == {"origin": "L2_MAIN_001"}
    # 다른 조건(제한·선호)은 유지
    assert s.editing_condition["constraints"]["max_duration_minutes"] is None
    assert s.editing_condition["preference"] is None


def test_release_station_slot_clears_slot_and_id():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_001", "시청")
    version = store.release_station_slot(s.session_id, "origin")
    assert version == 2
    assert s.editing_condition["stations"]["origin"] is None
    assert s.confirmed_stations == {}


def test_swap_moves_slots_and_ids_together():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_001", "시청")
    store.confirm_station_slot(s.session_id, "destination", "L5_539", "여의도")
    version = store.swap_stations(s.session_id)
    assert version == 3
    assert s.editing_condition["stations"]["origin"]["raw_name"] == "여의도"
    assert s.editing_condition["stations"]["destination"]["raw_name"] == "시청"
    assert s.confirmed_stations == {"origin": "L5_539", "destination": "L2_MAIN_001"}


def test_reset_conditions_keeps_confirmed_stations():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_001", "시청")
    assert store.reset_conditions(s.session_id) is True
    # 역은 유지 — editing_condition을 None으로 만들지 않고 stations를 살려둔다
    assert s.editing_condition is not None
    assert s.editing_condition["stations"]["origin"] == {"raw_name": "시청", "suggested_name": None}
    assert s.editing_condition["constraints"] == {
        "max_duration_minutes": None,
        "max_transfers": None,
        "max_fare_krw": None,
    }
    assert s.editing_condition["preference"] is None
    assert s.confirmed_condition is None
    assert s.confirmed_stations == {"origin": "L2_MAIN_001"}  # 역은 유지


def test_reset_conditions_keeps_stations_and_nulls_constraints_and_preference():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_001", "시청")
    store.confirm_station_slot(s.session_id, "destination", "L5_539", "여의도")
    store.begin_request(s.session_id, "r1")
    store.apply_ai_response(
        s.session_id,
        "r1",
        {
            "stations": {
                "origin": {"raw_name": "시청", "suggested_name": None},
                "destination": {"raw_name": "여의도", "suggested_name": None},
            },
            "constraints": {"max_duration_minutes": 40, "max_transfers": 1, "max_fare_krw": 5000},
            "preference": "fastest",
        },
    )
    version_before = s.version
    store.begin_request(s.session_id, "r-inflight")
    assert store.reset_conditions(s.session_id) is True
    assert s.version == version_before + 1
    # stations(확정 역)는 변경 전과 동일
    assert s.editing_condition["stations"] == {
        "origin": {"raw_name": "시청", "suggested_name": None},
        "destination": {"raw_name": "여의도", "suggested_name": None},
    }
    # constraints 세 키는 유지하되 값은 null, preference는 null
    assert s.editing_condition["constraints"] == {
        "max_duration_minutes": None,
        "max_transfers": None,
        "max_fare_krw": None,
    }
    assert s.editing_condition["preference"] is None
    # 확정 역 ID는 유지, inflight는 clear하지 않음 (version 불일치로 자동 폐기)
    assert s.confirmed_stations == {"origin": "L2_MAIN_001", "destination": "L5_539"}
    assert "r-inflight" in s.inflight


def test_reset_conditions_none_stays_none():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    assert store.reset_conditions(s.session_id) is True
    assert s.editing_condition is None  # None이면 그대로 None 유지
    assert s.version == 1


def test_new_search_clears_everything_and_inflight():
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_001", "시청")
    store.begin_request(s.session_id, "inflight-1")
    version = store.new_search(s.session_id)
    assert version == 2
    assert s.editing_condition is None  # 다음 AI 요청은 current_conditions: null
    assert s.confirmed_stations == {}
    assert s.inflight == {}  # 진행 중 해석 무효화
    # 무효화된 응답은 폐기된다
    assert store.apply_ai_response(s.session_id, "inflight-1", {"x": 1}) is False


def test_ttl_sweep_removes_idle_sessions():
    store = SessionStore(ttl_seconds=60)
    old = store.get_or_create(str(uuid.uuid4()))
    old.last_access -= 120
    store.get_or_create(str(uuid.uuid4()))
    assert store.apply_ai_response(old.session_id, "x", {}) is False


def test_get_session_dependency_header_flow(client):
    app = client.app

    @app.get("/_test/session")
    def _session(session=Depends(get_session)):
        return {"session_id": session.session_id, "version": session.version}

    sid = str(uuid.uuid4())
    r1 = client.get("/_test/session", headers={"X-Session-ID": sid})
    assert r1.status_code == 200
    assert r1.json()["session_id"] == sid
    assert r1.headers["X-Session-ID"] == sid

    r2 = client.get("/_test/session", headers={"X-Session-ID": sid})
    assert r2.json()["session_id"] == sid

    r3 = client.get("/_test/session")  # 헤더 없음 → 새 세션
    assert r3.json()["session_id"] != sid

    r4 = client.get("/_test/session", headers={"X-Session-ID": "not-a-uuid"})
    assert r4.json()["session_id"] != sid


def _slot(name, suggested=None):
    return {"raw_name": name, "suggested_name": suggested} if name is not None else None


def _conds(origin=None, destination=None):
    return {
        "stations": {"origin": _slot(origin), "destination": _slot(destination)},
        "constraints": {"max_duration_minutes": None, "max_transfers": None, "max_fare_krw": None},
        "preference": None,
    }


def test_ai_response_releases_confirmation_when_raw_name_changes():
    """이슈 #2: 문장으로 역을 바꾸면 기존 확정 역 ID가 해제된다."""
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.begin_request(s.session_id, "r1")
    store.apply_ai_response(s.session_id, "r1", _conds("강남", "여의도"))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_022", "강남")
    assert s.confirmed_stations == {"origin": "L2_MAIN_022"}
    # 문장으로 출발역을 잠실로 변경
    store.begin_request(s.session_id, "r2")
    ok = store.apply_ai_response(s.session_id, "r2", _conds("잠실", "여의도"))
    assert ok is True
    assert s.confirmed_stations == {}  # 출발역 확정 해제 — 경로 탐색이 예전 역으로 진행되지 않게
    assert s.editing_condition["stations"]["origin"]["raw_name"] == "잠실"


def test_ai_response_keeps_confirmation_when_raw_name_unchanged():
    """역 표현이 유지되면(교정 제안만 변경) 확정 ID는 유지된다 — 시간·요금·선호 수정과 동일."""
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.begin_request(s.session_id, "r1")
    store.apply_ai_response(s.session_id, "r1", _conds("강남", "여의도"))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_022", "강남")
    # 같은 raw_name + 교정 제안만 다른 응답
    store.begin_request(s.session_id, "r2")
    ok = store.apply_ai_response(
        s.session_id,
        "r2",
        {
            "stations": {"origin": _slot("강남", "강남역"), "destination": _slot("여의도")},
            "constraints": {"max_duration_minutes": 40, "max_transfers": None, "max_fare_krw": None},
            "preference": None,
        },
    )
    assert ok is True
    assert s.confirmed_stations == {"origin": "L2_MAIN_022"}  # 확정 유지


def test_ai_response_releases_confirmation_when_slot_becomes_null():
    """'출발역 초기화'처럼 슬롯이 null이 되면 확정 해제."""
    store = SessionStore()
    s = store.get_or_create(str(uuid.uuid4()))
    store.begin_request(s.session_id, "r1")
    store.apply_ai_response(s.session_id, "r1", _conds("강남", "여의도"))
    store.confirm_station_slot(s.session_id, "origin", "L2_MAIN_022", "강남")
    store.begin_request(s.session_id, "r2")
    store.apply_ai_response(s.session_id, "r2", _conds(None, "여의도"))
    assert s.confirmed_stations == {}
    assert s.editing_condition["stations"]["origin"] is None
