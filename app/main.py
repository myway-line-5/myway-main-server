"""FastAPI 앱 팩토리. 환경변수 검증은 app.config.Settings가 시작 시점에 수행한다."""

from __future__ import annotations

from fastapi import FastAPI

from .ai_client import AiClient
from .catalog import StationCatalog
from .config import Settings
from .db import Database, init_db
from .routers import conditions, interpret, stations
from .sessions import SessionStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    init_db(settings.db_path)
    db = Database(settings.db_path)

    app = FastAPI(
        title="MyWay 메인 서버",
        version="0.2.0",
    )
    app.state.settings = settings
    app.state.db = db
    app.state.session_store = SessionStore(ttl_seconds=settings.session_ttl_seconds)
    app.state.catalog = StationCatalog.load(db)
    app.state.ai_client = AiClient(
        base_url=settings.ai_server_base_url,
        token=settings.ai_server_auth_token,
        timeout_seconds=settings.ai_interpret_timeout_seconds,
    )

    app.include_router(stations.router, prefix="/v1")
    app.include_router(interpret.router, prefix="/v1")
    app.include_router(conditions.router, prefix="/v1")

    @app.get("/health", tags=["meta"], summary="헬스 체크")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
