"""자연어 문장 해석 API — AI 서비스를 경유하는 유일한 메인 엔드포인트.

상세설계서 표 4-9/4-12 반영:
- 메인→AI: POST {base}/v1/conditions/interpret, 2필드(current_conditions, user_message).
  최초·새 검색은 current_conditions: null (필드 자체를 null로 전송, 생략 아님).
- 응답 판정: 성공은 HTTP 200 + exception=false(boolean) + error_code=null + 전체 conditions.
  exception 필드가 없거나 boolean이 아니면 비정상 응답으로 미반영.
  exception=true면 업무 실패(error_code 전달). 401은 내부 인증 오류.
  413(본문 용량 초과)은 입력 한도 business_failure. 422는 본문의 error_code로 세분:
  INPUT_LIMIT_EXCEEDED(문장 길이 초과)만 business_failure, 그 밖(INVALID_REQUEST 등 요청 형식 오류)은
  메인 서버 문제이므로 communication_failure. AI 조건 검증 실패도 communication_failure.
  429/502/504/503은 AI 처리 오류(AI_UNAVAILABLE, 조건 유지). 그 외 비200·통신 실패도 조건 유지.
- X-AI-Provider가 기대 모드와 다르거나 없으면 조건을 반영하지 않는다 (mock은 모의 시험에서만).
  불일치는 설정 오류 가능성이 크므로 경고(warning) 로그로 남겨 시연 전에 바로 알 수 있게 한다.
- 같은 세션에 진행 중인 해석이 있으면 AI를 호출하지 않고 business_failure(REQUEST_IN_PROGRESS)로 즉시 실패.
- 자동 재시도 0회. 성공 시 세션 version/inflight 규칙으로 반영, 늦은 응답은 폐기.
- 앱에는 result로 success / business_failure / communication_failure / auth_error를 구분해 내려주고,
  conditions는 앱용 camelCase로 변환해서 전달한다. 문구는 앱이 만든다.
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, Request

from ..ai_client import AiClient, AiInterpretResult, AiTransportError
from ..conditions import to_app_conditions, validate_ai_conditions, validate_user_message
from ..schemas import InterpretRequest, InterpretResponse
from ..sessions import SessionState, get_session

router = APIRouter(tags=["conditions"])
log = logging.getLogger("myway.interpret")

# 표 4-12 상태 코드 분류
STATUS_BUSINESS = {413}  # 본문 용량 초과(입력 한도) → business_failure
STATUS_AI_UNAVAILABLE = {429, 502, 504, 503}  # AI 처리 오류 → communication_failure
# 422는 본문의 error_code로 세분한다: INPUT_LIMIT_EXCEEDED(문장 길이 초과)만 business_failure,
# 그 밖(INVALID_REQUEST 등 요청 형식 오류)은 메인이 보낸 요청이 틀린 서버 오류 → communication_failure
VALID_PROVIDERS = ("mock", "gemini")


@router.post(
    "/conditions/interpret",
    response_model=InterpretResponse,
    summary="자연어 문장 해석 (AI 서비스 경유)",
)
def interpret(
    req: InterpretRequest,
    request: Request,
    session: SessionState = Depends(get_session),
) -> InterpretResponse:
    t0 = time.perf_counter()
    store = request.app.state.session_store
    ai: AiClient = request.app.state.ai_client
    expected_provider: str = request.app.state.settings.expected_ai_provider

    def done(result: str, error_code: str | None, provider: str | None) -> InterpretResponse:
        # 토큰·개인정보(문장 내용) 제외, request_id와 소요시간만 기록
        log.info(
            "interpret request_id=%s result=%s error_code=%s total_ms=%.0f provider=%s",
            request_id,
            result,
            error_code,
            (time.perf_counter() - t0) * 1000,
            provider,
        )
        return InterpretResponse(result=result, error_code=error_code)  # type: ignore[arg-type]

    message_error = validate_user_message(req.user_message)
    if message_error:
        return InterpretResponse(result="business_failure", error_code=message_error)

    request_id = uuid.uuid4().hex
    base_version = store.begin_request(session.session_id, request_id)
    if base_version is None:
        # 같은 세션에 진행 중인 해석이 있음 — AI 호출 없이 바로 실패 (호출 분 낭비 방지)
        return done("business_failure", "REQUEST_IN_PROGRESS", None)
    # 최초·새 검색은 current_conditions: null. 그 외에는 현재 편집 조건(snake_case) 전체.
    current_conditions = session.editing_condition

    try:
        resp: AiInterpretResult = ai.interpret(current_conditions, req.user_message)
    except AiTransportError:
        store.abort_request(session.session_id, request_id)
        return done("communication_failure", None, None)

    provider = resp.provider

    if resp.status == 401:
        store.abort_request(session.session_id, request_id)
        return done("auth_error", None, provider)
    if resp.status in STATUS_BUSINESS:
        store.abort_request(session.session_id, request_id)
        code = resp.body.get("error_code") if isinstance(resp.body, dict) else None
        return done("business_failure", code if isinstance(code, str) else "INPUT_LIMIT_EXCEEDED", provider)
    if resp.status == 422:
        store.abort_request(session.session_id, request_id)
        code = resp.body.get("error_code") if isinstance(resp.body, dict) else None
        code = code if isinstance(code, str) and code else "INVALID_REQUEST"
        if code == "INPUT_LIMIT_EXCEEDED":
            # 문장 길이 초과 — 사용자가 입력을 조정해야 하는 사용자 오류
            return done("business_failure", code, provider)
        # INVALID_REQUEST 등 — 메인→AI 요청 형식 오류로 추정되는 서버 문제 (이슈 #3)
        log.warning(
            "interpret request_id=%s AI 422 error_code=%s — 메인→AI 요청 형식 오류로 추정, 조건 유지",
            request_id,
            code,
        )
        return done("communication_failure", code, provider)
    if resp.status in STATUS_AI_UNAVAILABLE:
        store.abort_request(session.session_id, request_id)
        # 표 4-12: 503 + SERVICE_NOT_CONFIGURED(설정 오류)와 503 + AI_UNAVAILABLE(처리 오류) 구분 —
        # 본문의 error_code가 문자열로 있으면 그대로 전달, 없으면 AI_UNAVAILABLE 폴백
        code = resp.body.get("error_code") if isinstance(resp.body, dict) else None
        return done("communication_failure", code if isinstance(code, str) and code else "AI_UNAVAILABLE", provider)
    if resp.status != 200 or not isinstance(resp.body, dict):
        store.abort_request(session.session_id, request_id)
        return done("communication_failure", None, provider)

    body = resp.body
    # exception 필드 필수 boolean — 누락·비boolean이면 비정상 응답으로 미반영
    if "exception" not in body or not isinstance(body.get("exception"), bool):
        store.abort_request(session.session_id, request_id)
        return done("communication_failure", "MALFORMED_RESPONSE", provider)

    if body["exception"] is True:
        store.abort_request(session.session_id, request_id)
        code = body.get("error_code")
        return done("business_failure", code if isinstance(code, str) and code else "UNKNOWN_ERROR", provider)

    # 성공 후보: error_code는 null이어야 함
    if body.get("error_code") is not None:
        store.abort_request(session.session_id, request_id)
        return done("communication_failure", "MALFORMED_RESPONSE", provider)

    # X-AI-Provider 기대 모드 검증 — 누락·불일치 시 미반영
    if provider not in VALID_PROVIDERS or provider != expected_provider:
        store.abort_request(session.session_id, request_id)
        # 설정 오류 가능성 — AI 호출은 이미 사용된 뒤라 경고 로그로 시연 전에 바로 알린다
        log.warning(
            "interpret request_id=%s AI provider mismatch (expected=%s actual=%s) — "
            "MYWAY_EXPECTED_AI_PROVIDER와 AI 서버 모드를 확인하세요. 호출은 폐기됨",
            request_id,
            expected_provider,
            provider,
        )
        return done("communication_failure", "AI_PROVIDER_MISMATCH", provider)

    error = validate_ai_conditions(body.get("conditions"))
    if error:
        store.abort_request(session.session_id, request_id)
        # AI가 반환한 조건이 메인 검증을 통과하지 못함 — AI 출력 문제지 사용자 오류가 아님 (이슈 #3)
        log.warning(
            "interpret request_id=%s AI 조건 검증 실패 error_code=%s — AI 응답 문제로 조건 유지",
            request_id,
            error,
        )
        return done("communication_failure", error, provider)

    applied = store.apply_ai_response(session.session_id, request_id, body["conditions"])
    if not applied:
        # 늦은 응답 — 폐기하고 마지막 정상 조건 유지
        return done("communication_failure", "STALE_RESPONSE_DISCARDED", provider)

    log.info(
        "interpret request_id=%s result=success total_ms=%.0f provider=%s",
        request_id,
        (time.perf_counter() - t0) * 1000,
        provider,
    )
    return InterpretResponse(
        result="success",
        conditions=to_app_conditions(body["conditions"]),
        ai_provider=provider,
    )
