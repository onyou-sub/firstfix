"""data/raw/의 CSV 5개를 정리해 data/processed/에 저장한다.

- 컬럼명: machineID -> machine_id, errorID -> error_id, failures.failure -> comp
- maint.by_failure: 같은 machine_id·datetime·comp의 고장 기록이 failures에 있으면 1, 없으면 0
- datetime은 'YYYY-MM-DD HH:MM:SS' 문자열로 저장
- machine_id, datetime 순으로 정렬
- 값 이름 변경 (config.py): comp1~4 -> COMP_MAP, error1~5 -> ERROR_MAP
  model1~4는 바꾸지 않는다.
data/raw/의 원본은 수정하지 않는다.
"""

import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DATA_DIR.parent))  # 프로젝트 루트의 config.py를 읽기 위해

from config import COMP_MAP, ERROR_MAP  # noqa: E402

RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

NAMES = ["telemetry", "errors", "maint", "failures", "machines"]
RENAMES = {
    "telemetry": {"machineID": "machine_id"},
    "errors": {"machineID": "machine_id", "errorID": "error_id"},
    "maint": {"machineID": "machine_id"},
    "failures": {"machineID": "machine_id", "failure": "comp"},
    "machines": {"machineID": "machine_id"},
}
VALUE_MAPS = {
    "errors": {"error_id": ERROR_MAP},
    "maint": {"comp": COMP_MAP},
    "failures": {"comp": COMP_MAP},
}
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def load_raw(name: str) -> pd.DataFrame:
    df = pd.read_csv(RAW_DIR / f"PdM_{name}.csv")
    df = df.rename(columns=RENAMES[name])
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
    for col, mapping in VALUE_MAPS.get(name, {}).items():
        unknown = set(df[col]) - set(mapping)
        if unknown:
            raise ValueError(f"{name}.{col}에 config.py 매핑에 없는 값이 있습니다: {sorted(unknown)}")
        df[col] = df[col].map(mapping)
    return df


def add_by_failure(maint: pd.DataFrame, failures: pd.DataFrame) -> pd.DataFrame:
    keys = ["machine_id", "datetime", "comp"]
    failure_keys = failures[keys].drop_duplicates().assign(by_failure=1)
    maint = maint.merge(failure_keys, on=keys, how="left")
    maint["by_failure"] = maint["by_failure"].fillna(0).astype(int)
    return maint


def finalize(df: pd.DataFrame) -> pd.DataFrame:
    sort_cols = [c for c in ["machine_id", "datetime"] if c in df.columns]
    # 안정 정렬: 같은 machine_id·datetime 안에서는 원본 순서를 유지한다.
    df = df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)
    if "datetime" in df.columns:
        df["datetime"] = df["datetime"].dt.strftime(DATETIME_FORMAT)
    return df


def main() -> int:
    missing = [f"PdM_{n}.csv" for n in NAMES if not (RAW_DIR / f"PdM_{n}.csv").is_file()]
    if missing:
        print(f"data/raw/에 없는 파일: {', '.join(missing)}")
        print("먼저 실행하세요: uv run python data/download.py")
        return 1

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    tables = {name: load_raw(name) for name in NAMES}
    tables["maint"] = add_by_failure(tables["maint"], tables["failures"])

    for name, df in tables.items():
        df = finalize(df)
        out_path = PROCESSED_DIR / f"{name}.csv"
        df.to_csv(out_path, index=False)
        print(f"저장: {out_path.relative_to(DATA_DIR.parent)}  {len(df):,}행  {list(df.columns)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 실행 방법 (프로젝트 루트에서, download.py 실행 후):
#   uv run python data/prepare.py
