"""data/processed/의 CSV 5개를 data/pdm.db에 테이블 5개로 적재한다.

- CSV 1개 = 테이블 1개. 합치거나 미리 조인하지 않고 행·컬럼을 그대로 옮긴다.
- CREATE TABLE을 직접 작성하고, 값은 컬럼 자료형에 맞게 변환해 INSERT한다.
- 같은 이름 테이블이 있으면 그 테이블만 DROP 후 다시 만든다.
  (다른 테이블, 예: 이후에 추가할 parts_stock·criticality는 건드리지 않는다.)
"""

import csv
import sqlite3
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = DATA_DIR / "pdm.db"

# 테이블별 (컬럼명, SQL 자료형, 변환 함수). 컬럼 순서는 CSV 헤더 순서와 같다.
TABLES = {
    "telemetry": [
        ("datetime", "TEXT", str),
        ("machine_id", "INTEGER", int),
        ("volt", "REAL", float),
        ("rotate", "REAL", float),
        ("pressure", "REAL", float),
        ("vibration", "REAL", float),
    ],
    "errors": [
        ("datetime", "TEXT", str),
        ("machine_id", "INTEGER", int),
        ("error_id", "TEXT", str),
    ],
    "maint": [
        ("datetime", "TEXT", str),
        ("machine_id", "INTEGER", int),
        ("comp", "TEXT", str),
        ("by_failure", "INTEGER", int),
    ],
    "failures": [
        ("datetime", "TEXT", str),
        ("machine_id", "INTEGER", int),
        ("comp", "TEXT", str),
    ],
    "machines": [
        ("machine_id", "INTEGER PRIMARY KEY", int),
        ("model", "TEXT", str),
        ("age", "INTEGER", int),
    ],
}
INDEXES = {
    "telemetry": "CREATE UNIQUE INDEX idx_telemetry_machine_datetime ON telemetry (machine_id, datetime)",
    "errors": "CREATE INDEX idx_errors_machine_datetime ON errors (machine_id, datetime)",
    "maint": "CREATE INDEX idx_maint_machine_datetime ON maint (machine_id, datetime)",
    "failures": "CREATE INDEX idx_failures_machine_datetime ON failures (machine_id, datetime)",
}


def read_rows(name: str) -> list[tuple]:
    columns = TABLES[name]
    csv_path = PROCESSED_DIR / f"{name}.csv"
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        expected = [col for col, _, _ in columns]
        if header != expected:
            raise ValueError(f"{csv_path.name} 헤더가 다릅니다: {header} (기대: {expected})")
        converters = [conv for _, _, conv in columns]
        # 빈 값이나 형식이 다른 값은 int()/float()에서 오류가 나므로 조용히 NULL로 들어가지 않는다.
        return [tuple(conv(v) for conv, v in zip(converters, row)) for row in reader]


def load_table(conn: sqlite3.Connection, name: str) -> int:
    rows = read_rows(name)
    columns = TABLES[name]
    col_defs = ",\n    ".join(f"{col} {sql_type}" for col, sql_type, _ in columns)
    placeholders = ", ".join("?" for _ in columns)

    # 테이블 하나를 한 트랜잭션으로 덮어쓴다. 중간에 실패하면 이전 테이블이 그대로 남는다.
    with conn:
        conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.execute(f"CREATE TABLE {name} (\n    {col_defs}\n)")
        conn.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)
        if name in INDEXES:
            conn.execute(INDEXES[name])
    return len(rows)


def main() -> int:
    missing = [f"{n}.csv" for n in TABLES if not (PROCESSED_DIR / f"{n}.csv").is_file()]
    if missing:
        print(f"data/processed/에 없는 파일: {', '.join(missing)}")
        print("먼저 실행하세요: uv run python data/prepare.py")
        return 1

    conn = sqlite3.connect(DB_PATH)
    try:
        print(f"적재 대상: {DB_PATH}")
        for name in TABLES:
            n_rows = load_table(conn, name)
            print(f"  {name:<10} {n_rows:>9,}행")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 실행 방법 (프로젝트 루트에서, prepare.py 실행 후):
#   uv run python data/load_db.py
#   uv run python data/check_db.py
