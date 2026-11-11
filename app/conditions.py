"""AI 계약 조건(snake_case) ↔ 앱 DTO(camelCase) 변환과 검증.

상세설계서 표 4-9/4-10: AI 조건 객체는 stations·constraints·preference를 항상 포함하고
snake_case이며, 메인이 앱 도메인(camelCase)과의 변환을 담당한다.
세션에는 AI 원본(snake_case)을 저장하고, 앱 응답에는 camelCase로 변환해서 내려준다.
"""
from __future__ import annotations

import unicodedata
from typing import Any

# 수치 경계: 시간 1~1,440분 · 환승 0~50회 · 요금 0~1,000,000원
CONSTRAINT_RANGES: dict[str, tuple[int, int]] = {
    "max_duration_minutes": (1, 1440),
    "max_transfers": (0, 50),
    "max_fare_krw": (0, 1_000_000),
}


def empty_conditions() -> dict[str, Any]:
    """제한 없음의 기본 조건 — 필수 필드는 값 null이어도 생략하지 않는다."""
    return {
        "stations": {"origin": None, "destination": None},
        "constraints": {k: None for k in CONSTRAINT_RANGES},
        "preference": None,
    }


def validate_user_message(message: Any) -> str | None:
    """user_message 검증: NFC 정규화 후 1~1,000자, 공백만 있으면 거부."""
    if not isinstance(message, str):
        return "INVALID_USER_MESSAGE"
    normalized = unicodedata.normalize("NFC", message)
    if not (1 <= len(normalized) <= 1000) or not normalized.strip():
        return "INVALID_USER_MESSAGE"
    return None


def validate_ai_conditions(conditions: Any) -> str | None:
    """AI가 반환한 conditions의 구조·수치 경계 검증. 오류 코드 또는 None.

    필수 키 누락·타입 오류·범위 밖 값이면 거부한다 (범위 밖 값을 줄여서 반영하지 않음).
    """
    if not isinstance(conditions, dict):
        return "INVALID_CONDITIONS"
    for key in ("stations", "constraints", "preference"):
        if key not in conditions:
            return "INVALID_CONDITIONS"
    constraints = conditions["constraints"]
    if not isinstance(constraints, dict) or not all(k in constraints for k in CONSTRAINT_RANGES):
        return "INVALID_CONDITIONS"
    for key, (lo, hi) in CONSTRAINT_RANGES.items():
        value = constraints[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
            return "INVALID_CONDITION_RANGE"
    stations = conditions["stations"]
    if not isinstance(stations, dict) or "origin" not in stations or "destination" not in stations:
        return "INVALID_CONDITIONS"
    for slot in ("origin", "destination"):
        s = stations[slot]
        if s is not None and (not isinstance(s, dict) or "raw_name" not in s):
            return "INVALID_CONDITIONS"
    return None


def to_app_conditions(snake: dict[str, Any] | None) -> dict[str, Any] | None:
    """앱용 camelCase 조건으로 변환 (메인 확정 역 station 등 메인 전용 필드는 제외)."""
    if snake is None:
        return None

    def slot(s: Any) -> dict[str, Any] | None:
        if s is None:
            return None
        return {"rawName": s.get("raw_name"), "suggestedName": s.get("suggested_name")}

    stations = snake.get("stations") or {}
    constraints = snake.get("constraints") or {}
    return {
        "origin": slot(stations.get("origin")),
        "destination": slot(stations.get("destination")),
        "constraints": {
            "maxDurationMinutes": constraints.get("max_duration_minutes"),
            "maxTransfers": constraints.get("max_transfers"),
            "maxFareKrw": constraints.get("max_fare_krw"),
        },
        "preference": snake.get("preference"),
    }
