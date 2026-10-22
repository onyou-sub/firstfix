"""가상 데이터와 매뉴얼 검색 인덱스를 확인한다.

- parts_stock 4행, qty=0 부품 1개 이상
- criticality 100행, 등급 비율, machines와 연결
- pdm.db 테이블 7개
- 매뉴얼 검색 테스트 4개 (1순위 출처가 기대한 파일인지)
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd

VIRTUAL_DIR = Path(__file__).resolve().parent
ROOT_DIR = VIRTUAL_DIR.parent.parent
sys.path.insert(0, str(VIRTUAL_DIR))

from build_index import search_manual_docs  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "pdm.db"
EXPECTED_TABLES = {"telemetry", "errors", "maint", "failures", "machines", "parts_stock", "criticality",
                   "work_standards", "teams"}
SEARCH_TESTS = [
    ("베어링 진동 상승 점검", "manual_bearing.md"),
    ("전압이 높을 때 점검 방법", "manual_motor.md"),
    ("부품 교체 후에도 이상이 계속됨", "manual_sensor.md"),
    ("점검 전 설비 정지 절차", "manual_safety.md"),
]

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def check_tables(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = EXPECTED_TABLES - tables
    check(f"DB 테이블 {len(EXPECTED_TABLES)}개 존재", not missing,
          f"없음: {sorted(missing)}" if missing else ", ".join(sorted(tables)))


def check_parts(conn: sqlite3.Connection) -> None:
    parts = pd.read_sql_query("SELECT * FROM parts_stock", conn)
    print("\n[parts_stock]")
    print(parts.to_string(index=False))
    check("parts_stock 4행", len(parts) == 4, f"{len(parts)}행")
    zero = parts.loc[parts["qty"] == 0, "comp"].tolist()
    check("qty=0 부품 1개 이상", len(zero) >= 1, f"재고 0: {zero}")


def check_criticality(conn: sqlite3.Connection) -> None:
    crit = pd.read_sql_query("SELECT * FROM criticality", conn)
    print("\n[criticality 등급 비율]")
    ratio = crit["criticality"].value_counts().sort_index()
    for grade, n in ratio.items():
        print(f"  {grade}: {n:>3}대 ({n / len(crit):.0%})")
    check("criticality 100행", len(crit) == 100, f"{len(crit)}행")
    check("등급 비율 A 20% / B 50% / C 30%", ratio.to_dict() == {"A": 20, "B": 50, "C": 30}, str(ratio.to_dict()))
    bottleneck_grades = set(crit.loc[crit["bottleneck"] == 1, "criticality"])
    check("bottleneck은 A등급 중 일부만", bottleneck_grades == {"A"} and crit["bottleneck"].sum() < ratio["A"],
          f"bottleneck {int(crit['bottleneck'].sum())}대")
    orphan = conn.execute(
        "SELECT COUNT(*) FROM criticality c LEFT JOIN machines m ON c.machine_id = m.machine_id"
        " WHERE m.machine_id IS NULL"
    ).fetchone()[0]
    check("criticality.machine_id가 모두 machines에 존재", orphan == 0, f"연결 안 됨 {orphan}건")


def check_search() -> None:
    print("\n[매뉴얼 검색 테스트] (1순위 출처)")
    for query, expected in SEARCH_TESTS:
        docs = search_manual_docs(query, k=3)
        top = docs[0].metadata if docs else {"source": "-", "section": "-"}
        check(f"'{query}' -> {expected}", top["source"] == expected, f"1순위: {top['source']} §{top['section']}")


def main() -> None:
    pd.set_option("display.unicode.east_asian_width", True)
    with sqlite3.connect(DB_PATH) as conn:
        check_tables(conn)
        check_parts(conn)
        check_criticality(conn)
    check_search()

    print(f"\n결과: {sum(results)}/{len(results)} 통과")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

# 실행 순서 (프로젝트 루트에서):
#   1. uv run python data/virtual/make_virtual.py
#   2. uv run python data/virtual/build_index.py    # .env에 OPENAI_API_KEY 필요
#   3. uv run python data/virtual/check_virtual.py
