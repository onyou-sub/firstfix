"""data/pdm.db를 검증(OK / FAIL)하고, 테이블 연결을 확인하는 샘플 조회를 출력한다."""

import sqlite3
import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
DB_PATH = DATA_DIR / "pdm.db"

# 이후 parts_stock, criticality를 추가하면 여기에도 추가한다.
EXPECTED_ROWS = {
    "telemetry": 876_100,
    "errors": 3_919,
    "maint": 3_286,
    "failures": 761,
    "machines": 100,
}
EXPECTED_BY_FAILURE = 743
INDEX_QUERY = (
    "SELECT * FROM telemetry "
    "WHERE machine_id = 1 AND datetime <= '2015-06-18 06:00:00'"
)

SAMPLE_MACHINE = 1
SAMPLE_START = "2015-06-17 00:00:00"
SAMPLE_END = "2015-06-20 00:00:00"  # 미포함 (2015-06-17 ~ 2015-06-19)

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    status = "OK  " if ok else "FAIL"
    print(f"[{status}] {label}" + (f"  ({detail})" if detail else ""))


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()[0]


def run_checks(conn: sqlite3.Connection) -> None:
    print("== 테이블 ==")
    tables = sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    check(
        f"테이블 정확히 {len(EXPECTED_ROWS)}개 ({', '.join(EXPECTED_ROWS)})",
        tables == sorted(EXPECTED_ROWS),
        f"실제 {tables}",
    )
    present = [t for t in EXPECTED_ROWS if t in tables]

    print("\n== 행 수 ==")
    for name, expected in EXPECTED_ROWS.items():
        if name not in present:
            check(f"{name} 행 수 {expected:,}", False, "테이블 없음")
            continue
        actual = scalar(conn, f"SELECT COUNT(*) FROM {name}")
        check(f"{name} 행 수 {expected:,}", actual == expected, f"실제 {actual:,}")

    if "maint" in present:
        print("\n== maint.by_failure ==")
        n = scalar(conn, "SELECT COUNT(*) FROM maint WHERE by_failure = 1")
        check(f"by_failure = 1 행 {EXPECTED_BY_FAILURE}개", n == EXPECTED_BY_FAILURE, f"실제 {n:,}")

    if "machines" in present:
        print("\n== machine_id 참조 (LEFT JOIN machines) ==")
        for name in ["errors", "maint", "failures", "telemetry"]:
            if name not in present:
                continue
            n_orphan = scalar(
                conn,
                f"SELECT COUNT(*) FROM {name} t "
                f"LEFT JOIN machines m ON t.machine_id = m.machine_id "
                f"WHERE m.machine_id IS NULL",
            )
            check(f"{name}.machine_id가 machines에 모두 존재", n_orphan == 0, f"없는 행 {n_orphan:,}")

    if "telemetry" in present:
        print("\n== 인덱스 사용 ==")
        plan = " / ".join(r[-1] for r in conn.execute(f"EXPLAIN QUERY PLAN {INDEX_QUERY}"))
        check("telemetry 조회에 'USING INDEX' 포함", "USING INDEX" in plan, plan)


def show(conn: sqlite3.Connection, title: str, sql: str, params: tuple = ()) -> None:
    print(f"\n-- {title}")
    df = pd.read_sql_query(sql, conn, params=params)
    print(df.to_string(index=False) if len(df) else "(없음)")


def run_samples(conn: sqlite3.Connection) -> None:
    period = (SAMPLE_MACHINE, SAMPLE_START, SAMPLE_END)
    where = "WHERE machine_id = ? AND datetime >= ? AND datetime < ?"
    print(f"\n== 샘플 조회: machine_id={SAMPLE_MACHINE}, 2015-06-17 ~ 2015-06-19 ==")
    show(
        conn,
        "vibration 일별 평균 (telemetry)",
        f"SELECT substr(datetime, 1, 10) AS date, ROUND(AVG(vibration), 2) AS avg_vibration, "
        f"COUNT(*) AS n_hours FROM telemetry {where} GROUP BY date ORDER BY date",
        period,
    )
    show(conn, "에러 (errors)", f"SELECT datetime, error_id FROM errors {where} ORDER BY datetime", period)
    show(conn, "고장 (failures)", f"SELECT datetime, comp FROM failures {where} ORDER BY datetime", period)
    show(conn, "교체 (maint)", f"SELECT datetime, comp, by_failure FROM maint {where} ORDER BY datetime", period)

    print("\n== 샘플 조회: model별 고장 건수 (failures JOIN machines) ==")
    show(
        conn,
        "model별 고장 건수",
        "SELECT m.model, COUNT(*) AS n_failures, COUNT(DISTINCT f.machine_id) AS n_machines "
        "FROM failures f JOIN machines m ON f.machine_id = m.machine_id "
        "GROUP BY m.model ORDER BY m.model",
    )


def main() -> int:
    if not DB_PATH.is_file():
        print(f"DB 파일이 없습니다: {DB_PATH}")
        print("먼저 실행하세요: uv run python data/load_db.py")
        return 1

    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        run_checks(conn)
        n_fail = results.count(False)
        print(f"\n결과: {len(results)}개 항목 중 OK {results.count(True)}개, FAIL {n_fail}개")
        run_samples(conn)
    finally:
        conn.close()
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

# 실행 방법 (프로젝트 루트에서, load_db.py 실행 후):
#   uv run python data/load_db.py
#   uv run python data/check_db.py
