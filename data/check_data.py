"""data/processed/의 CSV 5개를 기대값과 비교해 항목별로 OK / FAIL을 출력한다."""

import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DATA_DIR.parent))  # 프로젝트 루트의 config.py를 읽기 위해

from config import COMP_MAP, ERROR_MAP  # noqa: E402

PROCESSED_DIR = DATA_DIR / "processed"

EXPECTED_ROWS = {
    "telemetry": 876_100,
    "errors": 3_919,
    "maint": 3_286,
    "failures": 761,
    "machines": 100,
}
TELEMETRY_START = "2015-01-01 06:00:00"
TELEMETRY_END = "2016-01-01 06:00:00"
TELEMETRY_ROWS_PER_MACHINE = 8_761
EXPECTED_BY_FAILURE = 743

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    status = "OK  " if ok else "FAIL"
    print(f"[{status}] {label}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    missing = [n for n in EXPECTED_ROWS if not (PROCESSED_DIR / f"{n}.csv").is_file()]
    if missing:
        print(f"data/processed/에 없는 파일: {', '.join(f'{n}.csv' for n in missing)}")
        print("먼저 실행하세요: uv run python data/prepare.py")
        return 1

    tables = {name: pd.read_csv(PROCESSED_DIR / f"{name}.csv") for name in EXPECTED_ROWS}

    print("== 행 수 ==")
    for name, expected in EXPECTED_ROWS.items():
        actual = len(tables[name])
        check(f"{name} 행 수 {expected:,}", actual == expected, f"실제 {actual:,}")

    print("\n== 결측치 / 완전 중복 행 ==")
    for name, df in tables.items():
        n_null = int(df.isna().sum().sum())
        check(f"{name} 결측치 0개", n_null == 0, f"실제 {n_null:,}")
        n_dup = int(df.duplicated().sum())
        check(f"{name} 완전 중복 행 0개", n_dup == 0, f"실제 {n_dup:,}")

    print("\n== telemetry 기간 ==")
    telemetry = tables["telemetry"]
    start, end = telemetry["datetime"].min(), telemetry["datetime"].max()
    check(f"telemetry 시작 {TELEMETRY_START}", start == TELEMETRY_START, f"실제 {start}")
    check(f"telemetry 종료 {TELEMETRY_END}", end == TELEMETRY_END, f"실제 {end}")
    per_machine = telemetry.groupby("machine_id").size()
    check(
        f"telemetry 설비당 {TELEMETRY_ROWS_PER_MACHINE:,}행",
        bool((per_machine == TELEMETRY_ROWS_PER_MACHINE).all()),
        f"최소 {per_machine.min():,} / 최대 {per_machine.max():,}",
    )

    print("\n== maint.by_failure ==")
    n_by_failure = int((tables["maint"]["by_failure"] == 1).sum())
    check(f"by_failure == 1 행 {EXPECTED_BY_FAILURE}개", n_by_failure == EXPECTED_BY_FAILURE, f"실제 {n_by_failure:,}")

    print("\n== machine_id 참조 ==")
    machine_ids = set(tables["machines"]["machine_id"])
    for name in ["telemetry", "errors", "maint", "failures"]:
        unknown = set(tables[name]["machine_id"]) - machine_ids
        check(f"{name}.machine_id가 machines에 모두 포함", not unknown, f"없는 ID {len(unknown)}개")

    print("\n== 값 이름 (config.py) ==")
    for name, col, allowed in [
        ("errors", "error_id", set(ERROR_MAP.values())),
        ("maint", "comp", set(COMP_MAP.values())),
        ("failures", "comp", set(COMP_MAP.values())),
    ]:
        unknown = set(tables[name][col]) - allowed
        check(f"{name}.{col} 값이 {sorted(allowed)} 안에 있음", not unknown, f"그 밖의 값 {sorted(unknown)}")

    print("\n== 파일별 컬럼명과 자료형 ==")
    for name, df in tables.items():
        print(f"- {name}.csv")
        for col, dtype in df.dtypes.items():
            print(f"    {col:<12} {dtype}")

    n_fail = results.count(False)
    print(f"\n결과: {len(results)}개 항목 중 OK {results.count(True)}개, FAIL {n_fail}개")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

# 실행 방법 (프로젝트 루트에서, prepare.py 실행 후):
#   uv run python data/check_data.py
