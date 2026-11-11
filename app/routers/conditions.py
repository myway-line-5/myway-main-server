"""조건 변경 계열 API — 모두 X-Session-ID 사용, AI 재호출 없이 version만 갱신한다.

- POST /v1/conditions/reset       "조건 모두 초기화" — 제한·선호만 해제, 역은 유지
- POST /v1/conditions/new-search  "새 검색" — 현재 조건·확정 역·결과 초기화, 진행 중 해석 무효화
                                   (다음 AI 요청은 current_conditions: null)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..conditions import to_app_conditions
from ..schemas import ConditionUpdateResponse
from ..sessions import SessionState, get_session

router = APIRouter(tags=["conditions"])


@router.post(
    "/conditions/reset",
    response_model=ConditionUpdateResponse,
    summary="조건 모두 초기화 (제한·선호만 해제, 확정 역은 유지)",
)
def reset_conditions(
    request: Request,
    session: SessionState = Depends(get_session),
) -> ConditionUpdateResponse:
    store = request.app.state.session_store
    ok = store.reset_conditions(session.session_id)
    if not ok:
        return ConditionUpdateResponse(result="business_failure", error_code="SESSION_NOT_FOUND")
    return ConditionUpdateResponse(
        result="success", version=session.version, conditions=to_app_conditions(session.editing_condition)
    )


@router.post(
    "/conditions/new-search",
    response_model=ConditionUpdateResponse,
    summary="새 검색 (현재 조건·확정 역·결과 초기화, 진행 중 해석 무효화)",
)
def new_search(
    request: Request,
    session: SessionState = Depends(get_session),
) -> ConditionUpdateResponse:
    store = request.app.state.session_store
    version = store.new_search(session.session_id)
    if version is None:
        return ConditionUpdateResponse(result="business_failure", error_code="SESSION_NOT_FOUND")
    return ConditionUpdateResponse(result="success", version=version, conditions=None)
