"""Pydantic 요청/응답 DTO.

연동 명세는 FastAPI /docs다. 엔드포인트명·필드명·예시를 문서에서 읽기 쉽게 유지할 것.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class StationDTO(BaseModel):
    station_id: str = Field(
        ...,
        examples=["L2_MAIN_001"],
        description="전처리 산출물의 node_id를 그대로 사용",
    )
    name: str = Field(..., examples=["시청"])
    line: str = Field(
        ...,
        examples=["2"],
        description='노선 원본 문자열("2", "5", "9"). "호선" 접미사 변환은 앱 표시 계층 몫',
    )
    is_active: bool = Field(
        ..., description="데이터상 서비스 대상 여부. 검색 대상 아닌 역 안내 구분에 사용"
    )


class StationListResponse(BaseModel):
    items: list[StationDTO] = Field(
        default_factory=list,
        description="검색 결과가 없으면 빈 배열 (오류 아님)",
    )


class InterpretRequest(BaseModel):
    user_message: str = Field(
        ..., examples=["강남역에서 여의도까지 가장 빨리 가는 법"], description="사용자 자연어 문장"
    )


class InterpretResponse(BaseModel):
    result: Literal["success", "business_failure", "communication_failure", "auth_error"] = Field(
        ...,
        description=(
            "success=성공(condition 반영됨) / business_failure=업무 실패(error_code 전달. "
            "같은 세션에 진행 중인 해석이 있으면 AI 호출 없이 REQUEST_IN_PROGRESS) / "
            "communication_failure=통신 실패(타임아웃·전송 오류·늦은 응답 폐기·모드 불일치 — 기존 조건 유지) / "
            "auth_error=AI 서버 401 (인증 오류)"
        ),
    )
    error_code: str | None = Field(
        None,
        description="오류 코드. business_failure=AI 전달 코드 또는 메인 서버 자체 코드, "
        "communication_failure=AI_PROVIDER_MISMATCH·STALE_RESPONSE_DISCARDED 등",
    )
    conditions: dict[str, Any] | None = Field(
        None, description="success 시 세션에 반영된 현재 편집 조건"
    )
    ai_provider: str | None = Field(None, examples=["mock", "gemini"], description="X-AI-Provider 헤더 값")


class StationResolveRequest(BaseModel):
    raw_name: str = Field(..., examples=["을지로3가"], description="AI가 반환한 역 이름 원문 (보존해서 응답에 그대로 싣는다)")
    suggested_name: str | None = Field(
        None, examples=["을지로3가"], description="AI의 교정 제안. 확정이 아니며 원문과 구분해 전달한다"
    )


class StationResolveResponse(BaseModel):
    raw_name: str
    suggested_name: str | None
    candidates: list[StationDTO] = Field(
        ...,
        description="raw_name 대조 후보. 같은 group_id(동명) 후보는 병합하지 않고 모두 전달",
    )
    suggested_candidates: list[StationDTO] = Field(
        default_factory=list, description="suggested_name 대조 후보 (교정 제안용)"
    )


class StationConfirmRequest(BaseModel):
    slot: Literal["origin", "destination"] = Field(..., description="확정할 슬롯 (출발/도착)")
    station_id: str = Field(..., examples=["L2_MAIN_001"], description="사용자가 채택한 역의 station_id")


class StationConfirmResponse(BaseModel):
    result: Literal["success", "business_failure"]
    error_code: str | None = Field(None, description="STATION_NOT_FOUND / STATION_INACTIVE")
    station: StationDTO | None = None
    version: int | None = Field(None, description="반영 후 세션 version (version+1 규칙)")
    conditions: dict[str, Any] | None = Field(None, description="반영된 현재 편집 조건 (앱용 camelCase)")


class StationReleaseRequest(BaseModel):
    slot: Literal["origin", "destination"] = Field(..., description="해제할 슬롯 (출발/도착)")


class ConditionUpdateResponse(BaseModel):
    """조건 변경 계열 응답 (초기화·새 검색·역 해제·교환). AI 재호출 없이 version만 갱신."""

    result: Literal["success", "business_failure"]
    error_code: str | None = None
    version: int | None = Field(None, description="변경 후 세션 version")
    conditions: dict[str, Any] | None = Field(None, description="현재 편집 조건 (앱용 camelCase, 없으면 null)")


class RouteType(str, Enum):
    SHORTEST_TIME = "SHORTEST_TIME"
    MIN_FARE = "MIN_FARE"
    MIN_TRANSFERS = "MIN_TRANSFERS"
    PREFERENCE = "PREFERENCE"


class RouteSegmentDTO(BaseModel):
    """앱 RouteSegment에 대응. 탐색 함수 연동 시 이 DTO로 변환한다."""

    from_station: StationDTO
    to_station: StationDTO
    line_name: str = Field(..., examples=["2"], description="구간 노선 원본 문자열")
    seconds: int
    meters: int
    is_transfer: bool


class RouteResultDTO(BaseModel):
    """앱 RouteResult에 대응."""

    type: RouteType
    segments: list[RouteSegmentDTO]
    total_seconds: int
    total_meters: int
    total_fare_krw: int | None = Field(None, description="총 요금(원). 거리비례 운임 기준 (10km 이하 1,550원)")
    transfer_count: int
    fare_version: str | None = Field(None, description="요금 버전 (meta 테이블의 fare_version, 없으면 null)")


class RouteSearchRequest(BaseModel):
    """POST /v1/routes/search. 바디의 station_id가 없으면 세션의 확정 역(confirmed_stations)으로 탐색한다.

    optimization이 있으면 세션 preference보다 우선한다 (앱이 기준별로 여러 번 요청해서
    탭을 구성하는 용도). 없으면従来대로 세션 preference / 기본 shortest_time.
    """

    origin_station_id: str | None = Field(None, examples=["L2_MAIN_001"])
    destination_station_id: str | None = Field(None, examples=["L5_539"])
    optimization: Literal["shortest_time", "minimum_transfer", "minimum_fare"] | None = Field(
        None,
        examples=["shortest_time"],
        description="명시적 탐색 기준. shortest_time / minimum_transfer / minimum_fare",
    )


class RouteSearchResponse(BaseModel):
    """탐색 미완료(한도 초과 등)는 incomplete로 처리 — 결과를 숨기지 않는다."""

    status: Literal["ok", "no_route", "incomplete", "error"] = Field(
        ...,
        description="ok=정상 / no_route=제한을 만족하는 경로 없음 / incomplete=탐색 미완료 / error=입력 오류(error_code 참조)",
    )
    error_code: str | None = Field(
        None,
        description="error 시 코드: ORIGIN_NOT_CONFIRMED / DESTINATION_NOT_CONFIRMED / STATION_NOT_FOUND / STATION_INACTIVE / UNSUPPORTED_PREFERENCE / CONSTRAINTS_NOT_SUPPORTED",
    )
    route: RouteResultDTO | None = None
    metrics: dict[str, Any] | None = Field(
        None, description="탐색 실측 측정치 (현재는 메인 측 소요시간). 상태 수·우위 비교 횟수·최대 큐 크기는 4번 함수 metrics 반환 추가 후 반영 예정"
    )


class FirstLastDTO(BaseModel):
    """첫차·막차. 시간은 HH:mm (자정 넘김 24:xx許容)."""

    direction: str = Field(..., examples=["OUT"])
    first: str = Field(..., examples=["05:36"])
    last: str = Field(..., examples=["24:46"])
    day_type: str = Field(..., examples=["WEEKDAY"], description="WEEKDAY / SATURDAY / HOLIDAY")


class FacilityDTO(BaseModel):
    code: str = Field(..., examples=["elevator"])
    name: str = Field(..., examples=["엘리베이터"])
    where: str = Field("", description="위치 상세. 데이터에 없으면 빈 문자열")


class StationDetailResponse(BaseModel):
    """역 상세 (앱 StationDetail対応). 혼잡도는 데이터 미확보 시 빈 배열."""

    station: StationDTO
    first_last: list[FirstLastDTO] = Field(default_factory=list)
    # 요일(day_type) -> 방향 -> 출발시각(HH:mm) 목록
    timetable: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    # 0~23시 혼잡도 0~100. 데이터 없으면 빈 배열
    congestion: list[int] = Field(default_factory=list)
    facilities: list[FacilityDTO] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────
# 실시간 도착·위치정보 (Phase 2, 서울시 실시간 API 연동)
# ────────────────────────────────────────────────────────────────


class ArrivalTrainDTO(BaseModel):
    """탑승역에 도착 예정인 열차 1건 (서울시 realtimeArrivalList 1행)."""

    train_no: str = Field(
        ...,
        examples=["2227"],
        description="열차번호. 위치정보 trainNo와 동일 체계 — 탑승 열차를 고정할 때 이 값을 쓴다",
    )
    line: str = Field(..., examples=["2"], description="노선 문자열 (2/5/9만 전달)")
    direction: Literal["up", "down", "unknown"] = Field(
        ...,
        description="방면 정규화: up=상행/내선, down=하행/외선, unknown=API가 알려주지 않음",
    )
    direction_label: str = Field(
        ...,
        examples=["내선"],
        description="서울시 updnLine 원문 (상행/내선/하행/외선)",
    )
    destination: str = Field(
        ...,
        examples=["성수"],
        description="행선지(종착역, bstatnNm)",
    )
    heading: str = Field(
        ...,
        examples=["성수행 - 역삼방면"],
        description="도착지 방면 안내 원문 (trainLineNm)",
    )
    arrival_seconds: int = Field(
        ...,
        description="도착 예정 남은 시간(초). 0이면 전역 도착 등 — arrival_message 참조",
    )
    arrival_message: str = Field(
        ...,
        examples=["2분 20초 후"],
        description="도착 예정 안내 원문 (arvlMsg2)",
    )
    arrival_code: str = Field(
        ...,
        examples=["99"],
        description="서울시 arvlCd 원문 (99=시간 안내형, 5=전역 도착 등)",
    )
    train_kind: str = Field(
        ...,
        examples=["일반"],
        description="열차 종류 원문 (일반/급행 등)",
    )
    is_last_train: bool = Field(..., description="막차 여부 (lstcarAt=1)")
    received_at: str = Field(
        ...,
        description="서울시 수신 시각 원문 (제공 시점 기준 — 실제와 차이날 수 있음)",
    )


class ArrivalListResponse(BaseModel):
    """GET /v1/realtime/arrivals 응답."""

    status: Literal["ok", "unavailable", "not_configured", "error"] = Field(
        ...,
        description=(
            "ok=정상 / unavailable=서울시 응답 지연·누락·실패로 확인 불가(추정 모드 전환 근거) "
            "/ not_configured=메인에 API 키 미설정 / error=요청 자체 오류"
        ),
    )
    error_code: str | None = Field(
        None,
        examples=["SEOUL_TRANSPORT_ERROR"],
        description="unavailable·error 사유 코드. ok여도 서울시 원본 오류 코드가 같이 오면 참고용",
    )
    station: StationDTO | None = Field(None, description="요청한 역 정보 (unavailable에도 포함)")
    items: list[ArrivalTrainDTO] = Field(default_factory=list, description="도착 예정 열차 목록")
    received_at: str | None = Field(
        None,
        description="서울시 수신 시각 원문. unavailable이면 None",
    )


class TrainPositionDTO(BaseModel):
    """운행 중인 열차 1대의 현재 위치·상태 (서울시 realtimePositionList 1행)."""

    train_no: str = Field(..., examples=["9103"], description="열차번호")
    line: str = Field(..., examples=["9"], description="노선 문자열 (2/5/9)")
    station_name: str = Field(..., examples=["가양"], description="현재 위치 역명 (statnNm)")
    station_seoul_id: str | None = Field(
        None,
        examples=["1009000925"],
        description="현재 위치 역의 서울시 statnId 원문 (우리 station_id 체계와 다름)",
    )
    direction: Literal["up", "down", "unknown"] = Field(
        ...,
        description="방면 정규화: up=상행(0), down=하행(1)",
    )
    status: Literal["approaching", "arrived", "departed", "prev_station_departed", "unknown"] = (
        Field(
            ...,
            description=(
                "열차 상태 정규화: approaching=전역 진입(0), arrived=전역 도착(1), "
                "departed=전역 출발(2), prev_station_departed=전전역 출발(3), unknown=확인 불가"
            ),
        )
    )
    terminal: str = Field(..., examples=["개화"], description="행선지·종착역 (statnTnm)")
    is_express: bool = Field(..., description="급행 여부 (directAt=1)")
    received_at: str = Field(
        ...,
        description="서울시 수신 시각 원문 (제공 시점 기준 — 실제와 차이날 수 있음)",
    )


class TrainPositionResponse(BaseModel):
    """GET /v1/realtime/trains/{train_no} 응답."""

    status: Literal["ok", "unavailable", "not_configured", "error"] = Field(
        ...,
        description="ok=정상 / unavailable=확인 불가(지연·미운행·실패) / not_configured=키 미설정 / error=요청 오류",
    )
    error_code: str | None = Field(
        None,
        examples=["TRAIN_NOT_FOUND"],
        description=(
            "unavailable·error 사유. TRAIN_NOT_FOUND=해당 열차가 운행 정보에 없음(미운행·고정 실패 근거), "
            "SEOUL_* / 서울시 원본 코드=API 실패"
        ),
    )
    train: TrainPositionDTO | None = Field(None, description="열차 위치. unavailable이면 None")
    received_at: str | None = Field(None, description="서울시 수신 시각 원문")
