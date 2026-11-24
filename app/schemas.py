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
