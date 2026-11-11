"""DB 스키마 생성. 데이터 적재는 load_data.py가 담당한다.

사용: python scripts/init_db.py [db_path]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402

if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "myway.db"
    init_db(db_path)
    print(f"스키마 생성 완료: {db_path}")
