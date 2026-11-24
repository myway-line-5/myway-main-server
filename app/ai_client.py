from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class AiTransportError(Exception):
    """통신 실패 (타임아웃·연결 오류 등). 재시도 없이 폐기."""


@dataclass
class AiInterpretResult:
    status: int
    body: dict[str, Any] | None
    provider: str | None  # X-AI-Provider 헤더 (mock/gemini)


class AiClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    def interpret(
        self, current_conditions: dict[str, Any] | None, user_message: str
    ) -> AiInterpretResult:
        """POST {base}/v1/conditions/interpret — 계약 2필드(current_conditions, user_message)만 보낸다.

        최초·새 검색은 current_conditions를 null로 전송한다 (필드는 생략하지 않음).
        """
        payload = json.dumps(
            {"current_conditions": current_conditions, "user_message": user_message}
        ).encode("utf-8")
        req = urllib.request.Request(
            self._base_url + "/v1/conditions/interpret",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    # 비정상 바디(게이트웨이 HTML, 깨진 JSON)도 통신 실패로 처리 —
                    # 이 예외가 밖으로 새면 inflight가 잔류해서 세션이 잠김 (버그)
                    raise AiTransportError(f"invalid_json:{type(e).__name__}") from e
                return AiInterpretResult(
                    status=resp.status,
                    body=body if isinstance(body, dict) else None,
                    provider=resp.headers.get("X-AI-Provider"),
                )
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = None
            return AiInterpretResult(
                status=e.code,
                body=body if isinstance(body, dict) else None,
                provider=e.headers.get("X-AI-Provider") if e.headers else None,
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
            raise AiTransportError(type(e).__name__) from e
