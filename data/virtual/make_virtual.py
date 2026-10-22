"""교육용 가상 데이터(부품 재고, 설비 중요도)를 만들어 CSV와 data/pdm.db에 저장한다.

- parts_stock.csv : comp, part_name, qty, safety_stock, lead_time_days (4행)
  2015-10-01 06:00 적중 설비의 부품 중 하나는 qty=0 (데모에서 발주 요청 흐름이 나오도록)
- criticality.csv : machine_id, line, criticality, bottleneck (100행)
  A 20% / B 50% / C 30%, line L1~L4, bottleneck은 A등급 중 일부만 1
- pdm.db에는 parts_stock, criticality 테이블만 다시 만든다 (기존 5개 테이블은 건드리지 않음)
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

VIRTUAL_DIR = Path(__file__).resolve().parent
ROOT_DIR = VIRTUAL_DIR.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from config import COMP_LABEL_KO, COMP_MAP  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "pdm.db"
BACKTEST_PATH = ROOT_DIR / "reports" / "backtest.csv"
PARTS_PATH = VIRTUAL_DIR / "parts_stock.csv"
CRITICALITY_PATH = VIRTUAL_DIR / "criticality.csv"
WORK_STANDARDS_PATH = VIRTUAL_DIR / "work_standards.csv"
TEAMS_PATH = VIRTUAL_DIR / "teams.csv"

# 작업 표준 시간 (task_type, comp, duration_min, skill). comp가 빈 값이면 부품과 무관한 작업
WORK_STANDARDS = [
    ("replace", "motor", 120, "전기"),
    ("replace", "drive", 180, "기계"),
    ("replace", "pump", 120, "유체"),
    ("replace", "bearing", 90, "기계"),
    ("sensor_check", "", 60, "공통"),      # 센서·설치 이상 가능성 (사람 확인)
    ("temporary_check", "", 60, "공통"),   # 재고 0이라 교체 불가 -> 점검·임시 조치
]
TEAMS = [("1팀", "전기", "09:00", "18:00"), ("2팀", "기계", "09:00", "18:00"), ("3팀", "유체", "09:00", "18:00")]

COMPS = list(COMP_MAP.values())  # motor, drive, pump, bearing
DEMO_DATE = "2015-10-01"
# 재고 0으로 만들 부품의 우선 후보. motor는 2015-12-22 교체 직후 충돌 데모와 겹치지 않도록 피한다.
OUT_OF_STOCK_PREFERENCE = ["pump", "drive", "bearing", "motor"]
BASE_SEED = 42
GRADE_RATIO = {"A": 0.2, "B": 0.5, "C": 0.3}
LINES = ["L1", "L2", "L3", "L4"]
BOTTLENECK_SHARE_OF_A = 0.3  # A등급 중 병목 설비 비율

PART_SPEC = {  # comp: (safety_stock, lead_time_days)
    "motor": (2, 5),
    "drive": (2, 4),
    "pump": (1, 3),
    "bearing": (3, 2),
}


def demo_day_machines() -> tuple[list[int], list[tuple[int, str]]]:
    """reports/backtest.csv의 2015-10-01 행에서 (고위험 설비, 적중 설비:부품)을 읽는다."""
    if not BACKTEST_PATH.exists():
        sys.exit(f"{BACKTEST_PATH.relative_to(ROOT_DIR)}이 없습니다. 먼저 실행: uv run python ml/backtest.py")
    bt = pd.read_csv(BACKTEST_PATH, encoding="utf-8-sig", keep_default_na=False)
    row = bt.loc[bt["date"] == DEMO_DATE].iloc[0]

    def pairs(text: str) -> list[tuple[int, str]]:
        return [(int(m), c) for m, c in (t.split(":") for t in text.split())]

    flagged = pairs(row["high_risk_machines"])
    actual = set(pairs(row["actual_failures"]))
    hits = [p for p in flagged if p in actual]  # 고위험 & 예측 부품까지 실제 고장
    return [m for m, _ in flagged], hits


def make_parts_stock(hits: list[tuple[int, str]], rng: np.random.Generator) -> pd.DataFrame:
    hit_comps = {c for _, c in hits}
    out_of_stock = next(c for c in OUT_OF_STOCK_PREFERENCE if c in hit_comps)
    rows = []
    for comp in COMPS:
        safety, lead = PART_SPEC[comp]
        rows.append({
            "comp": comp,
            "part_name": f"{COMP_LABEL_KO[comp]} 교체 키트",
            "qty": 0 if comp == out_of_stock else int(rng.integers(1, 5)),  # 1~4
            "safety_stock": safety,
            "lead_time_days": lead,
        })
    return pd.DataFrame(rows)


def make_criticality(machine_ids: list[int], rng: np.random.Generator) -> pd.DataFrame:
    n = len(machine_ids)
    counts = {g: round(n * r) for g, r in GRADE_RATIO.items()}
    counts["B"] += n - sum(counts.values())  # 반올림 오차 보정
    grades = rng.permutation(np.concatenate([[g] * k for g, k in counts.items()]))
    lines = rng.permutation(np.resize(LINES, n))

    df = pd.DataFrame({"machine_id": machine_ids, "line": lines, "criticality": grades, "bottleneck": 0})
    a_ids = df.index[df["criticality"] == "A"]
    n_bottleneck = max(1, round(len(a_ids) * BOTTLENECK_SHARE_OF_A))
    df.loc[rng.choice(a_ids, size=n_bottleneck, replace=False), "bottleneck"] = 1
    return df


def build(seed: int, machine_ids: list[int], hits: list[tuple[int, str]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    return make_parts_stock(hits, rng), make_criticality(machine_ids, rng)


def save_to_db(parts: pd.DataFrame, crit: pd.DataFrame) -> None:
    """parts_stock, criticality만 지우고 다시 만든다. criticality.machine_id -> machines.machine_id"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DROP TABLE IF EXISTS parts_stock")
        conn.execute("DROP TABLE IF EXISTS criticality")
        conn.execute("""
            CREATE TABLE parts_stock (
                comp TEXT PRIMARY KEY,
                part_name TEXT NOT NULL,
                qty INTEGER NOT NULL CHECK (qty >= 0),
                safety_stock INTEGER NOT NULL,
                lead_time_days INTEGER NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE criticality (
                machine_id INTEGER PRIMARY KEY REFERENCES machines(machine_id),
                line TEXT NOT NULL,
                criticality TEXT NOT NULL CHECK (criticality IN ('A', 'B', 'C')),
                bottleneck INTEGER NOT NULL CHECK (bottleneck IN (0, 1))
            )""")
        conn.executemany("INSERT INTO parts_stock VALUES (?, ?, ?, ?, ?)", parts.itertuples(index=False, name=None))
        conn.executemany(
            "INSERT INTO criticality VALUES (?, ?, ?, ?)",
            [(int(m), l, c, int(b)) for m, l, c, b in crit.itertuples(index=False, name=None)],
        )


def save_schedule_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    """work_standards, teams를 CSV와 DB에 저장 (다른 테이블은 건드리지 않음)."""
    ws = pd.DataFrame(WORK_STANDARDS, columns=["task_type", "comp", "duration_min", "skill"])
    teams = pd.DataFrame(TEAMS, columns=["team", "skill", "shift_start", "shift_end"])
    ws.to_csv(WORK_STANDARDS_PATH, index=False, encoding="utf-8-sig")
    teams.to_csv(TEAMS_PATH, index=False, encoding="utf-8-sig")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DROP TABLE IF EXISTS work_standards")
        conn.execute("DROP TABLE IF EXISTS teams")
        conn.execute("""
            CREATE TABLE work_standards (
                task_type TEXT NOT NULL,
                comp TEXT NOT NULL,
                duration_min INTEGER NOT NULL CHECK (duration_min > 0),
                skill TEXT NOT NULL,
                PRIMARY KEY (task_type, comp)
            )""")
        conn.execute("""
            CREATE TABLE teams (
                team TEXT PRIMARY KEY,
                skill TEXT NOT NULL,
                shift_start TEXT NOT NULL,
                shift_end TEXT NOT NULL
            )""")
        conn.executemany("INSERT INTO work_standards VALUES (?, ?, ?, ?)", WORK_STANDARDS)
        conn.executemany("INSERT INTO teams VALUES (?, ?, ?, ?)", TEAMS)
    return ws, teams


def main() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        machine_ids = pd.read_sql_query("SELECT machine_id FROM machines ORDER BY machine_id", conn)["machine_id"].tolist()
    flagged, hits = demo_day_machines()
    print(f"{DEMO_DATE} 06:00 고위험 설비: {flagged}, 적중(설비:부품): {hits}")
    if not hits:
        sys.exit(f"{DEMO_DATE}에 적중 설비가 없어 재고 0 부품을 정할 수 없습니다")

    # 데모 날 고위험 설비들의 등급이 한 가지로 몰리면 seed를 1씩 올린다 (결과는 항상 같은 seed로 재현됨)
    for seed in range(BASE_SEED, BASE_SEED + 1000):
        parts, crit = build(seed, machine_ids, hits)
        demo_grades = crit.set_index("machine_id").loc[flagged, "criticality"]
        if demo_grades.nunique() >= 2:
            break
        print(f"  seed {seed}: 데모 설비 등급이 {demo_grades.iloc[0]} 하나로 몰려 다음 seed 시도")
    else:
        sys.exit("등급이 분산되는 seed를 찾지 못했습니다")
    print(f"사용한 seed: {seed}")

    parts.to_csv(PARTS_PATH, index=False, encoding="utf-8-sig")
    crit.to_csv(CRITICALITY_PATH, index=False, encoding="utf-8-sig")
    save_to_db(parts, crit)
    ws, teams = save_schedule_tables()

    pd.set_option("display.unicode.east_asian_width", True)
    print(f"\n[parts_stock] -> {PARTS_PATH.relative_to(ROOT_DIR)}")
    print(parts.to_string(index=False))
    print(f"\n[criticality] -> {CRITICALITY_PATH.relative_to(ROOT_DIR)}")
    print(crit["criticality"].value_counts().sort_index().to_string())
    print(f"bottleneck=1: {crit.loc[crit['bottleneck'] == 1, 'machine_id'].tolist()} (모두 A등급)")
    print(f"\n{DEMO_DATE} 고위험 설비 등급:")
    print(crit.set_index("machine_id").loc[flagged].to_string())
    print(f"\n[work_standards] -> {WORK_STANDARDS_PATH.relative_to(ROOT_DIR)}")
    print(ws.to_string(index=False))
    print(f"\n[teams] -> {TEAMS_PATH.relative_to(ROOT_DIR)}")
    print(teams.to_string(index=False))
    print(f"\nDB 저장: {DB_PATH.relative_to(ROOT_DIR)} (parts_stock, criticality, work_standards, teams)")


if __name__ == "__main__":
    main()

# 실행 순서 (프로젝트 루트에서, ml/backtest.py 실행 후):
#   1. uv run python data/virtual/make_virtual.py
#   2. uv run python data/virtual/build_index.py    # .env에 OPENAI_API_KEY 필요
#   3. uv run python data/virtual/check_virtual.py
