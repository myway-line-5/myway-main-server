"""실시간 도착정보·열차 위치정보 API (Phase 2, 상세설계서 4.2.5·표 4-20).

- GET /v1/realtime/arrivals?station_id=...&line=...  탑승역 방면별 도착 예정 열차 목록
- GET /v1/realtime/trains/{train_no}?line=...          고정한 열차의 현재 역·상태

서울시 API는 메인 서버만 호출한다. 지연·누락·실패는 값을 추측해 채우지 않고
'확인 불가'(unavailable) 상태로만 앱에 전달한다 — 앱은 이 근거로 추정 모드로 전환한다.
세션 불필요 (역 목록·경로 탐색과 동일하게 파라미터만으로 동작).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from ..catalog import StationCatalog
from ..schemas import (
    ArrivalListResponse,
    ArrivalTrainDTO,
    StationDTO,
    TrainPositionDTO,
    TrainPositionResponse,
)
from ..seoul_client import SEOUL_LINE_BY_ID, SeoulRealtimeClient

router = APIRouter(tags=["realtime"])
log = logging.getLogger("myway.realtime")

# 도착정보 updnLine 원문 → 방면 정규화 (위치정보는 '0'/'1' 숫자로 온다)
_ARRIVAL_UP = {"상행", "내선"}
_ARRIVAL_DOWN = {"하행", "외선"}
# 위치정보 trainSttus 원문 → 열차 상태 정규화 (2026-11-28 실측)
_POSITION_STATUS = {
    "0": "approaching",
    "1": "arrived",
    "2": "departed",
    "3": "prev_station_departed",
}
_SUPPORTED_LINES = ("2", "5", "9")


def _client(request: Request) -> SeoulRealtimeClient | None:
    return request.app.state.seoul_client  # 키 미설정이면 None (시작은 실패시키지 않는다)


def _arrival_direction(raw: str | None) -> str:
    if raw in _ARRIVAL_DOWN:
        return "down"
    if raw in _ARRIVAL_UP:
        return "up"
    return "unknown"


def _position_direction(raw: str | None) -> str:
    if raw == "1":
        return "down"
    if raw == "0":
        return "up"
    return "unknown"


def _arrival_dto(row: dict, line: str, received_at: str) -> ArrivalTrainDTO:
    return ArrivalTrainDTO(
        train_no=str(row.get("btrainNo") or ""),
        line=line,
        direction=_arrival_direction(row.get("updnLine")),
        direction_label=str(row.get("updnLine") or ""),
        destination=str(row.get("bstatnNm") or ""),
        heading=str(row.get("trainLineNm") or ""),
        arrival_seconds=int(row.get("barvlDt") or 0),
        arrival_message=str(row.get("arvlMsg2") or ""),
        arrival_code=str(row.get("arvlCd") or ""),
        train_kind=str(row.get("btrainSttus") or ""),
        is_last_train=str(row.get("lstcarAt")) == "1",
        received_at=received_at,
    )


@router.get(
    "/realtime/arrivals",
    response_model=ArrivalListResponse,
    summary="탑승역 방면별 실시간 도착 예정 열차 목록",
)
def list_arrivals(
    request: Request,
    station_id: str = Query(..., examples=["L5_MAIN_539"], description="역 ID"),
    line: str | None = Query(
        None, examples=["5"], description="선택: 노선 문자열(2/5/9)로 결과 필터"
    ),
) -> ArrivalListResponse:
    client = _client(request)
    if client is None:
        return ArrivalListResponse(status="not_configured", error_code="SEOUL_API_NOT_CONFIGURED")
    catalog: StationCatalog = request.app.state.catalog
    station: StationDTO | None = catalog.get(station_id)
    if station is None:
        log.info("realtime_arrivals station_id=%s -> STATION_NOT_FOUND", station_id)
        return ArrivalListResponse(status="error", error_code="STATION_NOT_FOUND")
    if line is not None and line not in _SUPPORTED_LINES:
        return ArrivalListResponse(status="error", error_code="UNSUPPORTED_LINE")

    result = client.get_arrivals(station.name)
    if result.status != "ok":
        log.info(
            "realtime_arrivals station=%s -> unavailable error=%s", station.name, result.error_code
        )
        return ArrivalListResponse(
            status="unavailable", error_code=result.error_code, station=station
        )

    items: list[ArrivalTrainDTO] = []
    for row in result.rows:
        if not isinstance(row, dict):
            continue
        # 환승역은 타 노선(신분당선 등) 행도 같이 오므로 서비스 대상 노선만 전달한다 (실측).
        seoul_line = SEOUL_LINE_BY_ID.get(str(row.get("subwayId")))
        if seoul_line is None:
            continue
        if line is not None and seoul_line != line:
            continue
        items.append(_arrival_dto(row, seoul_line, result.received_at or ""))

    log.info(
        "realtime_arrivals station=%s line=%s items=%d received_at=%s",
        station.name,
        line,
        len(items),
        result.received_at,
    )
    return ArrivalListResponse(
        status="ok", station=station, items=items, received_at=result.received_at
    )


@router.get(
    "/realtime/trains/{train_no}",
    response_model=TrainPositionResponse,
    summary="고정한 열차의 실시간 위치·상태",
)
def get_train_position(
    request: Request,
    train_no: str,
    line: str = Query(..., examples=["9"], description="노선 문자열(2/5/9) — 열차번호 단독 조회는 불가"),
) -> TrainPositionResponse:
    client = _client(request)
    if client is None:
        return TrainPositionResponse(status="not_configured", error_code="SEOUL_API_NOT_CONFIGURED")
    if line not in _SUPPORTED_LINES:
        return TrainPositionResponse(status="error", error_code="UNSUPPORTED_LINE")

    result = client.get_positions(line)
    if result.status != "ok":
        log.info(
            "realtime_position train=%s line=%s -> unavailable error=%s",
            train_no,
            line,
            result.error_code,
        )
        return TrainPositionResponse(status="unavailable", error_code=result.error_code)

    # 호선 단위 응답에서 열차번호로 고정한 열차를 찾는다.
    row = next(
        (r for r in result.rows if isinstance(r, dict) and str(r.get("trainNo")) == train_no),
        None,
    )
    if row is None:
        # 미운행·운행 종료·번호 오류. 앱은 이 근거로 열차 재고정을 유도한다.
        log.info("realtime_position train=%s line=%s -> TRAIN_NOT_FOUND", train_no, line)
        return TrainPositionResponse(status="unavailable", error_code="TRAIN_NOT_FOUND")

    train = TrainPositionDTO(
        train_no=train_no,
        line=line,
        station_name=str(row.get("statnNm") or ""),
        station_seoul_id=(str(row.get("statnId")) if row.get("statnId") else None),
        direction=_position_direction(row.get("updnLine")),
        status=_POSITION_STATUS.get(str(row.get("trainSttus")), "unknown"),
        terminal=str(row.get("statnTnm") or ""),
        is_express=str(row.get("directAt")) == "1",
        received_at=result.received_at or "",
    )
    log.info(
        "realtime_position train=%s line=%s station=%s status=%s",
        train_no,
        line,
        train.station_name,
        train.status,
    )
    return TrainPositionResponse(
        status="ok", train=train, received_at=result.received_at
    )
