"""SQLite 메인 DB. 표준 라이브러리 sqlite3만 사용 (ORM·드라이버 추가 설치 없음).

스키마:
- Station(station_id PK, group_id, name, line, is_active)
- RouteEdge(edge_id PK, from_station_id, to_station_id, seconds, meters, is_transfer)
  (SQL 예약어 FROM 회피를 위해 컬럼명은 from_station_id / to_station_id)
- meta(key PK, value) — 적재 시점에 data_version·fare_version·적재일·출처 기록
- 인덱스: Station(name)
"""
from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS Station (
    station_id TEXT PRIMARY KEY,
    group_id   TEXT,
    name       TEXT NOT NULL,
    line       TEXT NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS RouteEdge (
    edge_id         TEXT PRIMARY KEY,
    from_station_id TEXT NOT NULL REFERENCES Station(station_id),
    to_station_id   TEXT NOT NULL REFERENCES Station(station_id),
    seconds         INTEGER NOT NULL CHECK (seconds > 0),
    meters          INTEGER NOT NULL CHECK (meters > 0),
    is_transfer     INTEGER NOT NULL DEFAULT 0 CHECK (is_transfer IN (0, 1))
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_station_name ON Station(name);
"""


def init_db(db_path: str) -> None:
    """스키마 생성만 수행한다. 데이터 적재는 별도 단계다."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class Database:
    """공유 커넥션(check_same_thread=False) + Lock으로 스레드 안전성을 처리한다.

    FastAPI 엔드포인트는 스레드풀에서 동작하므로 커넥션 공유 시 이 Lock이 필요하다.
    """

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
