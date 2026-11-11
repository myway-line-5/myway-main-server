"""환경변수 기반 설정.

명시 필수 환경변수 (누락 시 서버 시작 실패):
- AI_SERVER_AUTH_TOKEN: 서버 간 공유 Bearer 토큰. 코드·로그·응답 어디에도 기록하지 않는다.
- AI_SERVER_BASE_URL: AI 서비스 base URL. http/https만 허용. 로컬 시연 기준 http://127.0.0.1:8000
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse


def load_dotenv(path: str = ".env") -> None:
    """표준 라이브러리만으로 .env를 읽어 환경변수에 반영 (기존 값은 덮어쓰지 않음)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class Settings:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        if env is None:
            load_dotenv()
            env = os.environ
        token = env.get("AI_SERVER_AUTH_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "AI_SERVER_AUTH_TOKEN 환경변수가 설정되지 않았습니다. "
                "서버를 시작할 수 없습니다. "
                '(생성: python -c "import secrets; print(secrets.token_urlsafe(32))")'
            )
        self.ai_server_auth_token = token

        base_url = env.get("AI_SERVER_BASE_URL", "").strip()
        if not base_url:
            raise RuntimeError(
                "AI_SERVER_BASE_URL 환경변수가 설정되지 않았습니다. "
                "서버를 시작할 수 없습니다."
            )
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError(
                "AI_SERVER_BASE_URL은 http/https URL이어야 합니다. "
                "서버를 시작할 수 없습니다."
            )
        self.ai_server_base_url = base_url.rstrip("/")

        timeout = float(env.get("AI_INTERPRET_TIMEOUT_SECONDS", "25"))
        if timeout <= 0:
            raise RuntimeError("AI_INTERPRET_TIMEOUT_SECONDS는 양수여야 합니다.")
        self.ai_interpret_timeout_seconds = timeout  # 기본 25초 = AI 처리 예산 20초 + 여유

        provider = env.get("MYWAY_EXPECTED_AI_PROVIDER", "").strip()
        if provider not in ("mock", "gemini"):
            raise RuntimeError(
                "MYWAY_EXPECTED_AI_PROVIDER 환경변수를 mock 또는 gemini로 명시해야 합니다. "
                "서버를 시작할 수 없습니다."
            )
        self.expected_ai_provider = provider

        self.db_path = env.get("MYWAY_DB_PATH", "myway.db")
        self.session_ttl_seconds = int(env.get("SESSION_TTL_SECONDS", "3600"))
