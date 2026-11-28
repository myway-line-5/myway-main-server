"""서울시 실시간 지하철 API 클라이언트 (열린데이터광장 OA-12764·OA-12601).

- 도착정보: realtimeStationArrival (역명 기반) — 응답 realtimeArrivalList
- 위치정보: realtimePosition (호선명 기반, 예: 9호선) — 응답 realtimePositionList
- URL: {base}/{인증키}/json/{서비스명}/0/{끝행}/{조회값}

호출 한도가 있는 유료성 공공 API라 성공 응답뿐 아니라 실패 응답도 짧게 캐시한다
(같은 역을 연속으로 물어보는 화면에서 한도를 태우지 않기 위함).
인증키는 절대 로그·예외 메시지에 노출하지 않는다.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger("myway.seoul")

DEFAULT_SEOUL_BASE_URL = "http://swopenAPI.seoul.go.kr/api/subway"

# 서울 subwayId → 우리 line 문자열 (환승역은 타 노선 행도 같이 오므로 이 표로 거른다)
SEOUL_LINE_BY_ID = {
    "1002": "2",
    "1005": "5",
    "1009": "9",
}

# 위치정보 조회값은 '2호선' 형태
SEOUL_LINE_NAME = {
    "2": "2호선",
    "5": "5호선",
    "9": "9호선",
}

# 한 번에 받을 최대 행 수 (9호선 전체 열차 수를 커버하면 충분)
_SEOUL_END_ROW = 100


class SeoulTransportError(Exception):
    """통신 실패 (타임아웃·연결 오류·비정상 응답). 상위에서 재시도하지 않는다."""


@dataclass
class SeoulRows:
    """서울시 응답 1건. rows는 원본 행 dict 리스트 (필터·변환은 라우터 담당)."""

    status: str  # "ok" | "unavailable"
    rows: list
    error_code: str | None
    received_at: str | None  # 서울시 recptnDt 원문 (제공 시각, 응답마다 1~2분차이 남)


def _unavailable(error_code: str) -> SeoulRows:
    return SeoulRows(status="unavailable", rows=[], error_code=error_code, received_at=None)


class SeoulRealtimeClient:
    """TTL 캐시를 가진 서울시 실시간 지하철 API 클라이언트."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        cache_ttl_seconds: float,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, SeoulRows]] = {}

    def get_arrivals(self, station_name: str) -> SeoulRows:
        """역명으로 도착 예정 열차 목록 (realtimeStationArrival)."""
        return self._fetch("realtimeStationArrival", station_name)

    def get_positions(self, line: str) -> SeoulRows:
        """호선명으로 운행 중 열차 위치 목록 (realtimePosition)."""
        line_name = SEOUL_LINE_NAME.get(line)
        if line_name is None:
            return self._cached("realtimePosition:" + line, _unavailable("UNSUPPORTED_LINE"))
        return self._fetch("realtimePosition", line_name)

    # ── 내부 ─────────────────────────────────────────────────────────────

    def _cached(self, key: str, result: SeoulRows) -> SeoulRows:
        """성공·실패 모두 TTL 안에서 재사용한다 (한도·장애 전파 방지)."""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self._cache_ttl:
            return hit[1]
        self._cache[key] = (now, result)
        return result

    def _fetch(self, service: str, query: str) -> SeoulRows:
        key = service + ":" + query
        cached = self._cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < self._cache_ttl:
            return cached[1]

        url = "{}/{}/json/{}/0/{}/{}".format(
            self._base_url,
            self._api_key,
            service,
            _SEOUL_END_ROW,
            urllib.parse.quote(query),
        )
        body = self._request(url, service)
        if isinstance(body, SeoulRows):  # _request 내부에서 이미 실패로 확정된 경우
            return self._cached(key, body)

        result = self._parse(body)
        return self._cached(key, result)

    def _request(self, url: str, service: str):
        """서울시에 GET 요청. 실패는 예외가 아니라 SeoulRows(unavailable)로 돌린다.

        url에는 인증키가 포함돼 있으므로 로그·예외에 url을 절대 담지 않는다.
        """
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as e:
            logger.info("seoul http error service=%s status=%s", service, e.code)
            return _unavailable("SEOUL_HTTP_{}".format(e.code))
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            # 타임아웃·연결 실패 — url·예외 메시지에 키가 없으므로 유형명만 남긴다
            logger.info("seoul transport error service=%s type=%s", service, type(e).__name__)
            return _unavailable("SEOUL_TRANSPORT_ERROR")

        if status != 200:
            return _unavailable("SEOUL_HTTP_{}".format(status))
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.info("seoul invalid json service=%s", service)
            return _unavailable("SEOUL_INVALID_RESPONSE")
        return body

    def _parse(self, body) -> SeoulRows:
        """서울시 응답을 검증한다. 오류 응답은 두 형태가 섞여 온다 (실측).

        - errorMessage 객체: {"errorMessage": {"status": 500, "code": "ERROR-336", ...}}
        - 평탄한 형태: {"status": 500, "code": "ERROR-336", ...}
        둘 다 확인해야 한다.
        """
        if not isinstance(body, dict):
            return _unavailable("SEOUL_INVALID_RESPONSE")

        error_message = body.get("errorMessage")
        if isinstance(error_message, dict):
            code = error_message.get("code")
            status = error_message.get("status")
        else:
            code = body.get("code")
            status = body.get("status")

        if code is not None and code != "INFO-000":
            logger.info("seoul api error code=%s status=%s", code, status)
            return _unavailable(str(code))
        if status is not None and isinstance(status, int) and status != 200:
            # code 없이 status만 오는 오류 형태
            logger.info("seoul api error status=%s", status)
            return _unavailable("SEOUL_API_{}".format(status))

        rows_key = (
            "realtimeArrivalList" if "realtimeArrivalList" in body else "realtimePositionList"
        )
        rows = body.get(rows_key)
        if not isinstance(rows, list):
            logger.info("seoul rows missing key=%s", rows_key)
            return _unavailable("SEOUL_INVALID_RESPONSE")

        received_at = None
        for row in rows:
            if isinstance(row, dict) and row.get("recptnDt"):
                received_at = str(row["recptnDt"])
                break
        return SeoulRows(status="ok", rows=rows, error_code=None, received_at=received_at)
