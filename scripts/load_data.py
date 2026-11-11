"""전처리 산출물(CSV)을 메인 DB(SQLite)에 적재한다.

- data/는 2·5호선 전처리 산출물 (myway-android repo file 브랜치).
- manifest.json은 산출물에 없어 임시로 작성한 것. 정식 manifest는 추후 요청.
- 적재 검증: 필수 컬럼 존재·참조 일치·수치 양수만.
- Station·RouteEdge를 트랜잭션으로 통째로 교체한다 — 재적재만으로 데이터 갱신 가능.

사용: python scripts/load_data.py [db_path] [data_dir]
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database, init_db  # noqa: E402

REQUIRED_STATION_COLUMNS = {"node_id", "station_name", "line"}
REQUIRED_EDGE_COLUMNS = {"edge_id", "from_node", "to_node", "travel_time_sec", "distance_m", "transfer_count_increment"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load(db_path: str, data_dir: Path) -> dict[str, int]:
    stations_rows = _read_csv(data_dir / "stations.csv")
    edges_rows = _read_csv(data_dir / "graph_edges.csv")

    for rows, required, name in (
        (stations_rows, REQUIRED_STATION_COLUMNS, "stations.csv"),
        (edges_rows, REQUIRED_EDGE_COLUMNS, "graph_edges.csv"),
    ):
        if not rows or not required.issubset(rows[0].keys()):
            raise ValueError(f"{name}: 필수 컬럼이 없습니다 (필요: {sorted(required)})")

    stations: list[tuple] = []
    for r in stations_rows:
        # group_id 잠정 기준: 같은 역명 = 같은 그룹
        stations.append((r["node_id"], r["station_name"], r["station_name"], r["line"], 1))

    station_ids = {s[0] for s in stations}
    edges: list[tuple] = []
    for r in edges_rows:
        seconds, meters = int(r["travel_time_sec"]), int(r["distance_m"])
        if seconds <= 0 or meters <= 0:
            raise ValueError(f"수치 양수 검증 실패: {r['edge_id']} seconds={seconds} meters={meters}")
        if r["from_node"] not in station_ids or r["to_node"] not in station_ids:
            raise ValueError(f"참조 일치 검증 실패: {r['edge_id']} {r['from_node']} -> {r['to_node']}")
        is_transfer = 1 if int(r["transfer_count_increment"]) > 0 else 0
        edges.append((r["edge_id"], r["from_node"], r["to_node"], seconds, meters, is_transfer))

    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    loaded_at = datetime.now(timezone.utc).isoformat()

    init_db(db_path)
    db = Database(db_path)
    try:
        with db._lock:  # 트랜잭션으로 갈아끼기
            db._conn.execute("BEGIN")
            db._conn.execute("DELETE FROM RouteEdge")
            db._conn.execute("DELETE FROM Station")
            db._conn.executemany(
                "INSERT INTO Station (station_id, group_id, name, line, is_active) VALUES (?, ?, ?, ?, ?)",
                stations,
            )
            db._conn.executemany(
                "INSERT INTO RouteEdge (edge_id, from_station_id, to_station_id, seconds, meters, is_transfer)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                edges,
            )
            meta = {
                "data_version": str(manifest.get("data_version", "unknown")),
                "fare_version": str(manifest.get("fare_version", "unknown")),
                "covered_lines": ",".join(manifest.get("covered_lines", [])),
                "적재일": loaded_at,
                "출처": str(manifest.get("source", "unknown")),
            }
            db._conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(meta.items())
            )
            db._conn.commit()
    except Exception:
        db._conn.rollback()
        raise
    finally:
        db.close()
    return {"stations": len(stations), "edges": len(edges)}


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "myway.db"
    data_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data"
    counts = load(db_path, data_dir)
    print(f"적재 완료: {db_path} — Station {counts['stations']}개, RouteEdge {counts['edges']}개")
