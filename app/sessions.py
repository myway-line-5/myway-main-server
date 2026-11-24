"""세션 기반 조건 상태 관리 (이 서버의 핵심 책임). 상세설계서 4.2.3 기준:

- 앱이 매 요청 X-Session-ID(UUID4 문자열) 헤더로 세션 ID를 보낸다. 계정·로그인·인증 없음.
- 저장은 메모리 dict[session_id] + threading.Lock. DB·파일 영속화 없음. 미사용 세션은 TTL(기본 1시간) 후 제거.
- 세션 상태: editing_condition(AI 성공 응답만 반영, snake_case 원본) /
  confirmed_condition(사용자 확인 마친 탐색 조건) / confirmed_stations(slot → station_id) /
  version(정수, 상태 변경 시 단조 증가) / inflight(request_id -> 기반 version).
- AI 응답은 inflight 기반 version이 현재 version과 일치할 때만 반영하고 version+1.
  불일치·타임아웃·취소·검증 실패는 폐기하고 마지막 정상 조건을 유지한다.
- 같은 세션의 동시 요청: 진행 중인 inflight가 있으면 새 요청은 기록하지 않고 None을 반환한다.
  호출자는 AI 호출 없이 즉시 실패(REQUEST_IN_PROGRESS)로 응답해 호출 분 낭비를 막는다.
- 역 확정: 사용자 확인 시 editing_condition의 해당 슬롯을 {raw_name: 선택 역명,
  suggested_name: null}로 갱신하고 version+1. AI 재호출 없음.
- 조건 모두 초기화: 제한·선호만 해제하고 역은 유지한다.
- 새 검색: 현재 조건·확정 역·결과를 모두 초기화하고 진행 중 해석(inflight)을 무효화한다.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Header, Request, Response

from .conditions import CONSTRAINT_RANGES, empty_conditions

SESSION_HEADER = "X-Session-ID"
SLOTS = ("origin", "destination")


@dataclass
class SessionState:
    session_id: str
    editing_condition: dict[str, Any] | None = None  # AI snake_case 조건, 성공 응답만 반영
    confirmed_condition: dict[str, Any] | None = None  # 사용자 확인 마친 탐색 조건
    confirmed_stations: dict[str, str] = field(default_factory=dict)  # slot -> station_id
    version: int = 0
    inflight: dict[str, int] = field(default_factory=dict)  # request_id -> AI 호출 시점의 기반 version
    last_access: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionState] = {}

    def _sweep(self, now: float) -> None:
        expired = [k for k, s in self._sessions.items() if now - s.last_access > self._ttl]
        for k in expired:
            del self._sessions[k]

    def get_or_create(self, session_id: str | None) -> SessionState:
        """유효한 세션 ID면 해당 세션을 돌려주고, 없으면 그 ID로 새 세션을 만든다."""
        with self._lock:
            now = time.monotonic()
            self._sweep(now)
            key = session_id or str(uuid.uuid4())
            if key in self._sessions:
                s = self._sessions[key]
                s.last_access = now
                return s
            s = SessionState(session_id=key, last_access=now)
            self._sessions[key] = s
            return s

    def begin_request(self, session_id: str, request_id: str) -> int | None:
        """AI 호출 시작. 기반 version을 inflight에 기록하고 그 값을 반환한다.

        같은 세션에 이미 진행 중인 요청(inflight)이 있으면 기록하지 않고 None을 반환한다.
        호출자는 None이면 AI를 호출하지 않고 즉시 실패로 응답해야 한다.
        """
        with self._lock:
            s = self._sessions[session_id]
            s.last_access = time.monotonic()
            if s.inflight:
                return None
            s.inflight[request_id] = s.version
            return s.version

    def apply_ai_response(
        self, session_id: str, request_id: str, conditions: dict[str, Any]
    ) -> bool:
        """AI 성공 응답을 editing_condition에 반영한다.

        기반 version이 현재 version과 다르면(늦은 응답) 폐기하고 False를 반환한다.
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or request_id not in s.inflight:
                return False
            base_version = s.inflight.pop(request_id)
            s.last_access = time.monotonic()
            if base_version != s.version:
                return False  # 늦은 응답 폐기 — 마지막 정상 조건 유지
            s.editing_condition = conditions
            s.version += 1
            return True

    def abort_request(self, session_id: str, request_id: str) -> None:
        """실패·취소된 요청의 inflight 기록만 제거한다. version은 올리지 않는다."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return
            s.last_access = time.monotonic()
            s.inflight.pop(request_id, None)

    def confirm_station_slot(
        self, session_id: str, slot: str, station_id: str, station_name: str
    ) -> int | None:
        """사용자 확인으로 역을 채택한다 — AI 재호출 없음.

        editing_condition의 해당 슬롯을 {raw_name: 선택 역명, suggested_name: null}로
        갱신하고 confirmed_stations에 slot → station_id를 보관한 뒤 version+1 한다.
        """
        if slot not in SLOTS:
            return None
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.last_access = time.monotonic()
            cond = dict(s.editing_condition) if s.editing_condition else empty_conditions()
            stations = dict(cond.get("stations") or {"origin": None, "destination": None})
            stations[slot] = {"raw_name": station_name, "suggested_name": None}
            cond["stations"] = stations
            s.editing_condition = cond
            s.confirmed_stations[slot] = station_id
            s.version += 1
            return s.version

    def release_station_slot(self, session_id: str, slot: str) -> int | None:
        """명시적 역 해제 — 해당 슬롯과 확정 ID를 모두 해제한다. version+1."""
        if slot not in SLOTS:
            return None
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.last_access = time.monotonic()
            if s.editing_condition is not None:
                cond = dict(s.editing_condition)
                stations = dict(cond.get("stations") or {})
                stations[slot] = None
                cond["stations"] = stations
                s.editing_condition = cond
            s.confirmed_stations.pop(slot, None)
            s.version += 1
            return s.version

    def swap_stations(self, session_id: str) -> int | None:
        """교환 — 출발/도착 슬롯과 그에 대응하는 확정 ID를 함께 맞바꾼다. version+1."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.last_access = time.monotonic()
            if s.editing_condition is not None:
                cond = dict(s.editing_condition)
                stations = dict(cond.get("stations") or {})
                stations["origin"], stations["destination"] = (
                    stations.get("destination"),
                    stations.get("origin"),
                )
                cond["stations"] = stations
                s.editing_condition = cond
            if "origin" in s.confirmed_stations or "destination" in s.confirmed_stations:
                s.confirmed_stations["origin"], s.confirmed_stations["destination"] = (
                    s.confirmed_stations.get("destination"),
                    s.confirmed_stations.get("origin"),
                )
            s.version += 1
            return s.version

    def reset_conditions(self, session_id: str) -> bool:
        """'조건 모두 초기화' — 제한·선호만 해제하고 역은 유지한다.

        editing_condition이 None이 아니면 stations 슬롯(확정된 raw_name 포함)은 그대로 두고
        constraints 세 키와 preference만 null로 재구성해서 다시 저장한다 (계약상 필수 필드 생략 금지).
        None이면 그대로 None을 유지한다. confirmed_stations와 version+1은 기존 동작 유지,
        inflight는 clear하지 않는다 (진행 중 해석은 version 불일치로 자동 폐기).
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return False
            s.last_access = time.monotonic()
            if s.editing_condition is not None:
                cond = dict(s.editing_condition)
                cond["stations"] = dict(cond.get("stations") or {"origin": None, "destination": None})
                cond["constraints"] = {k: None for k in CONSTRAINT_RANGES}
                cond["preference"] = None
                s.editing_condition = cond
            s.confirmed_condition = None
            s.version += 1
            return True

    def confirm_conditions(self, session_id: str) -> int | None:
        """탐색 직전에 현재 편집 조건을 확정 탐색 조건으로 복사한다. version+1."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.last_access = time.monotonic()
            s.confirmed_condition = copy.deepcopy(s.editing_condition)
            s.version += 1
            return s.version

    def new_search(self, session_id: str) -> int | None:
        """새 검색 — 현재 조건·확정 역·결과를 모두 초기화하고 진행 중 해석(inflight)을 무효화한다.

        다음 AI 요청은 current_conditions: null이다. version+1.
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.last_access = time.monotonic()
            s.editing_condition = None
            s.confirmed_condition = None
            s.confirmed_stations = {}
            s.inflight.clear()
            s.version += 1
            return s.version


def is_valid_session_id(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        return uuid.UUID(raw).version == 4
    except (ValueError, AttributeError, TypeError):
        return False


def get_session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


async def get_session(
    response: Response,
    store: SessionStore = Depends(get_session_store),
    x_session_id: Annotated[
        str | None,
        Header(
            alias="X-Session-ID",
            description="세션 ID (UUID4 문자열). 없거나 형식이 다르면 새 세션을 발급하고 응답 헤더로 돌려준다",
        ),
    ] = None,
) -> SessionState:
    """유일한 세션 ID 추출 지점이다. 엔드포인트에서 헤더를 직접 읽지 않는다.

    헤더 파라미터로 선언해서 /docs(연동 명세)에 자동 노출되게 한다.
    """
    session = store.get_or_create(x_session_id if is_valid_session_id(x_session_id) else None)
    response.headers[SESSION_HEADER] = session.session_id
    return session
