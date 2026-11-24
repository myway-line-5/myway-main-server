"""역 상세 데이터 적재 — temp-data의 원본 CSV를 SQLite에 적재한다.

적재 대상 (2·5·9호선만):
- 서울교통공사_서울 도시철도 열차운행시각표_*.csv → TrainSchedule + StationFirstLast
- 서울교통공사_편의시설 현황_*.csv → StationFacility

역 매칭: 역사명+호선 → stations.csv의 station_name+line 1차,
        역사코드(fare_station_code, 앞 0 제거) 2차 fallback.

사용: python scripts/load_detail_data.py [db_path] [temp_data_dir]
기본 temp_data_dir: <repo>/../temp-data
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database, init_db  # noqa: E402

TARGET_LINES = ("2", "5", "9")

# 주중주말 코드 → day_type
DAY_TYPE_MAP = {"DAY": "WEEKDAY", "SAT": "SATURDAY", "END": "HOLIDAY"}

# 편의시설 CSV 컬럼 → (facility_code, facility_name)
FACILITY_COLUMNS = {
    "문화공간여부": ("cultural_space", "문화공간"),
    "휠체어리프트여부": ("wheelchair_lift", "휠체어 리프트"),
    "만남의장소여부": ("meeting_place", "만남의 장소"),
    "환승주차장여부": ("transfer_parking", "환승 주차장"),
    "자전거보관소여부": ("bike_parking", "자전거 보관소"),
    "엘리베이터여부": ("elevator", "엘리베이터"),
    "유인보관소(티러기지)여부": ("manned_luggage", "유인 보관소"),
    "무인민원발급기여부": ("unmanned_civil_service", "무인 민원발급기"),
    "환전키오스크여부": ("exchange_kiosk", "환전 키오스크"),
    "수유실여부": ("nursing_room", "수유실"),
}


def _read_csv(path: Path, encoding: str = "euc-kr") -> list[dict[str, str]]:
    with open(path, encoding=encoding, newline="") as f:
        return list(csv.DictReader(f))


def load_station_index(db: Database) -> dict[tuple[str, str], str]:
    """(name, line) → station_id 매칭 인덱스. 괄호 포함 역명은 괄호 제거 버전도 함께 등록."""
    import re
    index: dict[tuple[str, str], str] = {}
    for r in db.query_all("SELECT station_id, name, line FROM Station"):
        name, line, sid = r["name"], r["line"], r["station_id"]
        index[(name, line)] = sid
        # "흑석(중앙대입구)" → "흑석" 별칭도 등록 (원본이 중복되지 않게)
        short = re.sub(r"\(.*?\)", "", name).strip()
        if short and short != name:
            index.setdefault((short, line), sid)
    return index


def load_facility_index(db: Database) -> dict[str, str]:
    """fare_station_code(0 제거) → station_id 매칭 인덱스 (fallback)."""
    index: dict[str, str] = {}
    for r in db.query_all("SELECT station_id, fare_station_code FROM Station"):
        pass  # Station 테이블에 fare_station_code 컬럼 없음 — stations.csv에서 별도 로드
    return index


def match_station(
    name: str, line: str, station_index: dict[tuple[str, str], str], code_index: dict[str, str], code: str | None
) -> str | None:
    sid = station_index.get((name, line))
    if sid:
        return sid
    if code:
        return code_index.get(code.lstrip("0") or code)
    return None


def load_detail(db_path: str, temp_dir: Path, stations_csv: Path | None = None) -> dict[str, int]:
    init_db(db_path)
    db = Database(db_path)

    # stations.csv에서 fare_station_code 매핑 로드 (fallback용)
    code_index: dict[str, str] = {}
    if stations_csv and stations_csv.exists():
        for r in _read_csv(stations_csv, encoding="utf-8-sig"):
            code = (r.get("fare_station_code") or "").strip()
            if code:
                code_index[code] = r["node_id"]

    station_index = load_station_index(db)
    counts = {"schedule": 0, "facility": 0, "firstlast": 0, "unmatched_schedule": 0, "unmatched_facility": 0}

    try:
        with db._lock:
            db._conn.execute("BEGIN")
            db._conn.execute("DELETE FROM TrainSchedule")
            db._conn.execute("DELETE FROM StationFacility")
            db._conn.execute("DELETE FROM StationFirstLast")

            # --- 열차運行시각표 ---
            timetable_files = sorted(temp_dir.glob("서울교통공사_서울 도시철도 열차운행시각표_*.csv"))
            if not timetable_files:
                raise FileNotFoundError(f"열차운행시각표 CSV 없음: {temp_dir}")
            schedule_rows: list[tuple] = []
            first_last: dict[tuple[str, str, str, str], list[str]] = {}

            for row in _read_csv(timetable_files[0]):
                line = row["호선"].strip()
                if line not in TARGET_LINES:
                    continue
                name = row["역사명"].strip()
                code = row["역사코드"].strip()
                sid = match_station(name, line, station_index, code_index, code)
                if not sid:
                    counts["unmatched_schedule"] += 1
                    continue
                day_type = DAY_TYPE_MAP.get(row["주중주말"].strip(), "WEEKDAY")
                direction = row["방향"].strip()
                is_express = 1 if row["급행여부"].strip() == "1" else 0
                dep = row["열차출발시간"].strip() or None
                arr = row["열차도착시간"].strip() or None
                schedule_rows.append(
                    (sid, line, day_type, direction, is_express, arr, dep,
                     row["출발역"].strip() or None, row["도착역"].strip() or None)
                )
                if dep:
                    key = (sid, line, direction, day_type)
                    first_last.setdefault(key, []).append(dep)

            db._conn.executemany(
                "INSERT INTO TrainSchedule (station_id, line, day_type, direction, is_express,"
                " arrival_time, departure_time, origin_station, destination_station)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                schedule_rows,
            )
            counts["schedule"] = len(schedule_rows)

            # 첫차·막차 집계
            fl_rows = []
            for (sid, line, direction, day_type), times in first_last.items():
                fl_rows.append((sid, line, direction, day_type, min(times), max(times)))
            db._conn.executemany(
                "INSERT INTO StationFirstLast (station_id, line, direction, day_type, first_departure, last_departure)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                fl_rows,
            )
            counts["firstlast"] = len(fl_rows)

            # --- 편의시설 (서울교통공사 + 9호선1단계 등 *편의시설*.csv 모두) ---
            facility_files = sorted(temp_dir.glob("*편의시설*.csv"))
            if facility_files:
                fac_rows: list[tuple] = []
                for fac_file in facility_files:
                    for row in _read_csv(fac_file):
                        raw_line = row["호선"].strip()  # "02호선", "25호선", "41호선" 등
                        name = row["역명"].strip()
                        code = row["역번호"].strip()
                        # "41호선"은 9호선(수인분당선系열 코드)! → 9로 매핑
                        if raw_line == "41호선":
                            line_nums = ["9"]
                        else:
                            # "02호선" → ["2"], "25호선" → ["2","5"] (환승역)
                            line_nums = [c for c in raw_line if c.isdigit()]
                        sid = None
                        for ln in line_nums:
                            sid = match_station(name, ln, station_index, code_index, code)
                            if sid:
                                break
                        if not sid:
                            # 역명만으로 전체 검색
                            for (n, l), s in station_index.items():
                                if n == name:
                                    sid = s
                                    break
                        if not sid:
                            counts["unmatched_facility"] += 1
                            continue
                        for col, (fcode, fname) in FACILITY_COLUMNS.items():
                            if col in row:
                                avail = 1 if row[col].strip().upper() == "Y" else 0
                                fac_rows.append((sid, fcode, fname, avail))
                db._conn.executemany(
                    "INSERT OR REPLACE INTO StationFacility (station_id, facility_code, facility_name, available)"
                    " VALUES (?, ?, ?, ?)",
                    fac_rows,
                )
                counts["facility"] = len(fac_rows)

            db._conn.commit()
    except Exception:
        db._conn.rollback()
        raise
    finally:
        db.close()
    return counts


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "myway.db"
    temp_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "temp-data"
    stations_csv = ROOT / "data" / "stations.csv"
    counts = load_detail(db_path, temp_dir, stations_csv)
    print(f"적재 완료: {db_path}")
    print(f"  TrainSchedule: {counts['schedule']}건 (미매칭 {counts['unmatched_schedule']}건)")
    print(f"  StationFirstLast: {counts['firstlast']}건")
    print(f"  StationFacility: {counts['facility']}건 (미매칭 역 {counts['unmatched_facility']}개)")
