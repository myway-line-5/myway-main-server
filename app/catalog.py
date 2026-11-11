"""역 카탈로그.

시작 시(또는 적재 직후) Station 전체를 DB에서 읽어 메모리에 올리고,
역 목록·검색 API는 메모리에서 응답한다 (DB I/O 없음).
DB는 원천 데이터 보관용, 메모리는 작업용.
"""
from __future__ import annotations

from .db import Database
from .schemas import StationDTO


class StationCatalog:
    def __init__(self, stations: list[StationDTO]) -> None:
        self._stations = stations
        self._by_id = {s.station_id: s for s in stations}

    def name_of(self, station_id: str) -> str | None:
        s = self._by_id.get(station_id)
        return s.name if s else None

    def get(self, station_id: str) -> StationDTO | None:
        return self._by_id.get(station_id)

    @classmethod
    def load(cls, db: Database) -> "StationCatalog":
        rows = db.query_all(
            "SELECT station_id, name, line, is_active FROM Station ORDER BY name, line, station_id"
        )
        return cls(
            [
                StationDTO(
                    station_id=r["station_id"],
                    name=r["name"],
                    line=r["line"],
                    is_active=bool(r["is_active"]),
                )
                for r in rows
            ]
        )

    def all(self) -> list[StationDTO]:
        return list(self._stations)

    def search(self, query: str) -> list[StationDTO]:
        q = query.strip()
        if not q:
            return self.all()
        # 동일 이름의 다중 결과(동명 다른 노선)를 line 정보와 함께 모두 반환한다
        return [s for s in self._stations if q in s.name]
