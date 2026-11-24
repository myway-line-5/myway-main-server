"""역 목록·검색·확정 API.

- GET  /v1/stations                전체 역 목록 (세션 없이 동작)
- GET  /v1/stations/search?q=      이름 검색 (세션 없이 동작, 0건=빈 배열)
- GET  /v1/stations/{station_id}/detail  역 상세 (첫차·막차·시간표·편의시설, 세션 없이 동작)
- POST /v1/stations/resolve        AI 역 이름(raw/suggested) 대조 → 후보 목록 (세션 없이 동작)
- POST /v1/stations/confirm        역 확정 — 세션 사용, AI 재호출 없음
- POST /v1/stations/release        명시적 역 해제 (해당 슬롯 + 확정 ID 해제)
- POST /v1/stations/swap           교환 (출발/도착 슬롯과 확정 ID를 함께 맞바꿈)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..conditions import to_app_conditions
from ..schemas import (
    ConditionUpdateResponse,
    FacilityDTO,
    FirstLastDTO,
    StationConfirmRequest,
    StationConfirmResponse,
    StationDetailResponse,
    StationListResponse,
    StationReleaseRequest,
    StationResolveRequest,
    StationResolveResponse,
)
from ..sessions import SessionState, get_session

router = APIRouter(tags=["stations"])


def _hm(t: str | None) -> str:
    """HH:mm:ss → HH:mm (자정 넘김 24:xx 유지)."""
    if not t:
        return ""
    parts = t.split(":")
    return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else t


@router.get("/stations", response_model=StationListResponse, summary="전체 역 목록 조회")
def list_stations(request: Request) -> StationListResponse:
    return StationListResponse(items=request.app.state.catalog.all())


@router.get("/stations/search", response_model=StationListResponse, summary="역 이름 검색")
def search_stations(
    request: Request,
    q: str = Query("", description="검색어. 포함 일치, 결과가 없으면 빈 배열(items: [])을 반환한다"),
) -> StationListResponse:
    return StationListResponse(items=request.app.state.catalog.search(q))


@router.get(
    "/stations/{station_id}/detail",
    response_model=StationDetailResponse,
    summary="역 상세 (첫차·막차·시간표·편의시설)",
)
def station_detail(
    station_id: str,
    request: Request,
    day_type: str | None = Query(None, description="시간표 필터 요일 (WEEKDAY/SATURDAY/HOLIDAY). 생략 시 전체"),
    direction: str | None = Query(None, description="시간표 필터 방향 (UP/DOWN/IN/OUT). 생략 시 전체"),
    limit: int = Query(500, ge=1, le=2000, description="방면별 시간표 최대 건수"),
) -> StationDetailResponse:
    catalog = request.app.state.catalog
    db = request.app.state.db
    station = catalog.get(station_id)
    if station is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail={"error_code": "STATION_NOT_FOUND"})

    # 첫차·막차
    fl_rows = db.query_all(
        "SELECT line, direction, day_type, first_departure, last_departure "
        "FROM StationFirstLast WHERE station_id = ? ORDER BY line, day_type, direction",
        (station_id,),
    )
    first_last = [
        FirstLastDTO(
            direction=r["direction"],
            first=_hm(r["first_departure"]),
            last=_hm(r["last_departure"]),
            day_type=r["day_type"],
        )
        for r in fl_rows
    ]

    # 시간표: 요일 → 방면 → 출발시각 목록
    timetable: dict[str, dict[str, list[str]]] = {}
    sql = (
        "SELECT day_type, direction, departure_time FROM TrainSchedule "
        "WHERE station_id = ? AND departure_time IS NOT NULL"
    )
    params: list = [station_id]
    if day_type:
        sql += " AND day_type = ?"
        params.append(day_type.upper())
    if direction:
        sql += " AND direction = ?"
        params.append(direction.upper())
    sql += " ORDER BY day_type, direction, departure_time LIMIT 20000"
    for r in db.query_all(sql, tuple(params)):
        dt = r["day_type"]
        dr = r["direction"]
        bucket = timetable.setdefault(dt, {}).setdefault(dr, [])
        if len(bucket) < limit:
            bucket.append(_hm(r["departure_time"]))

    # 편의시설 (available=1만)
    fac_rows = db.query_all(
        "SELECT facility_code, facility_name FROM StationFacility "
        "WHERE station_id = ? AND available = 1 ORDER BY facility_name",
        (station_id,),
    )
    facilities = [FacilityDTO(code=r["facility_code"], name=r["facility_name"], where="") for r in fac_rows]

    return StationDetailResponse(
        station=station,
        first_last=first_last,
        timetable=timetable,
        congestion=[],  # 혼잡도 데이터 미확보
        facilities=facilities,
    )


@router.post(
    "/stations/resolve",
    response_model=StationResolveResponse,
    summary="AI가 반환한 역 이름 대조 — 후보 목록 응답",
)
def resolve_station(req: StationResolveRequest, request: Request) -> StationResolveResponse:
    """원문 raw_name·교정 제안 suggested_name을 구분해 처리한다.

    교정은 suggested_name만 제시하고 원문을 보존한다. 같은 group_id(동명 역) 후보는
    병합하지 않고 그대로 모두 전달한다.
    """
    catalog = request.app.state.catalog
    suggested = (
        catalog.search(req.suggested_name)
        if req.suggested_name and req.suggested_name != req.raw_name
        else []
    )
    return StationResolveResponse(
        raw_name=req.raw_name,
        suggested_name=req.suggested_name,
        candidates=catalog.search(req.raw_name),
        suggested_candidates=suggested,
    )


@router.post(
    "/stations/confirm",
    response_model=StationConfirmResponse,
    summary="역 확정 (사용자 채택 station_id 반영 — AI 재호출 없음)",
)
def confirm_station(
    req: StationConfirmRequest,
    request: Request,
    session: SessionState = Depends(get_session),
) -> StationConfirmResponse:
    catalog = request.app.state.catalog
    station = catalog.get(req.station_id)
    if station is None:
        return StationConfirmResponse(result="business_failure", error_code="STATION_NOT_FOUND")
    if not station.is_active:
        return StationConfirmResponse(result="business_failure", error_code="STATION_INACTIVE")

    store = request.app.state.session_store
    version = store.confirm_station_slot(
        session.session_id, req.slot, station.station_id, station.name
    )
    return StationConfirmResponse(
        result="success",
        station=station,
        version=version,
        conditions=to_app_conditions(session.editing_condition),
    )


@router.post(
    "/stations/release",
    response_model=ConditionUpdateResponse,
    summary="명시적 역 해제 (해당 슬롯과 확정 ID를 모두 해제)",
)
def release_station(
    req: StationReleaseRequest,
    request: Request,
    session: SessionState = Depends(get_session),
) -> ConditionUpdateResponse:
    store = request.app.state.session_store
    version = store.release_station_slot(session.session_id, req.slot)
    if version is None:
        return ConditionUpdateResponse(result="business_failure", error_code="SESSION_NOT_FOUND")
    return ConditionUpdateResponse(
        result="success", version=version, conditions=to_app_conditions(session.editing_condition)
    )


@router.post(
    "/stations/swap",
    response_model=ConditionUpdateResponse,
    summary="교환 (출발/도착 슬롯과 확정 ID 대응을 함께 맞바꿈)",
)
def swap_stations(
    request: Request,
    session: SessionState = Depends(get_session),
) -> ConditionUpdateResponse:
    store = request.app.state.session_store
    version = store.swap_stations(session.session_id)
    if version is None:
        return ConditionUpdateResponse(result="business_failure", error_code="SESSION_NOT_FOUND")
    return ConditionUpdateResponse(
        result="success", version=version, conditions=to_app_conditions(session.editing_condition)
    )
