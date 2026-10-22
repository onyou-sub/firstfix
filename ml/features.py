"""data/pdm.db에서 고장 예측용 특징(27개)과 라벨을 만든다. 모델 학습은 하지 않는다.

- build_training_table(): telemetry 한 행((machine_id, datetime)) = 한 줄, 특징 + 라벨 -> data/features.parquet
- build_features(as_of): as_of 시각의 설비 100대 특징 (예측용, 라벨 없음)

두 함수는 같은 내부 함수 _compute_features()로 특징을 계산한다.
모든 특징은 그 시각 "이하"의 정보만 사용한다.
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ML_DIR = Path(__file__).resolve().parent
ROOT_DIR = ML_DIR.parent
sys.path.insert(0, str(ROOT_DIR))  # 프로젝트 루트의 config.py를 읽기 위해

from config import COMP_MAP, ERROR_MAP  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "pdm.db"
FEATURES_PATH = ROOT_DIR / "data" / "features.parquet"

KEYS = ["machine_id", "datetime"]
SENSORS = ["volt", "rotate", "pressure", "vibration"]
WINDOWS = ["3h", "24h"]
ERROR_TYPES = list(ERROR_MAP.values())  # overvoltage, low_rpm, torque, pressure, vibration
COMPS = list(COMP_MAP.values())  # motor, drive, pump, bearing (라벨 우선순위 순서)
LABEL_HORIZON = pd.Timedelta(hours=24)
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

SENSOR_COLS = [f"{s}_{stat}_{w}" for s in SENSORS for w in WINDOWS for stat in ["mean", "std"]]
ERROR_COLS = [f"err_{e}_24h" for e in ERROR_TYPES]
DAYS_SINCE_COLS = [f"days_since_{c}" for c in COMPS]
FEATURE_COLS = SENSOR_COLS + ERROR_COLS + DAYS_SINCE_COLS + ["model", "age"]
LABEL_COLS = ["label"] + [f"y_{c}" for c in COMPS]


def _read(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    df = pd.read_sql_query(sql, conn, params=params)
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], format=DATETIME_FORMAT)
    return df


def _compute_features(
    telemetry: pd.DataFrame,
    errors: pd.DataFrame,
    maint: pd.DataFrame,
    machines: pd.DataFrame,
    targets: pd.DataFrame,
) -> pd.DataFrame:
    """targets의 (machine_id, datetime)마다 FEATURE_COLS를 계산한다.

    학습용·예측용이 모두 이 함수를 쓴다. 입력 표에 targets 시각 이후의 행이 있으면
    그 시각의 특징에 섞이지 않도록, 모든 창은 (t - 창 길이, t] 범위만 본다.
    """
    targets = targets[KEYS].drop_duplicates()

    # 1) 센서·에러 이동 집계: telemetry 시각 + 에러 시각 + 대상 시각을 한 시간축에 모은 뒤
    #    설비별 시간 기반 rolling (center=False, 현재 시각 포함)
    err_onehot = pd.get_dummies(errors["error_id"]).reindex(columns=ERROR_TYPES, fill_value=0)
    err_onehot.columns = ERROR_COLS
    err_counts = pd.concat([errors[KEYS], err_onehot.astype(int)], axis=1).groupby(KEYS, as_index=False).sum()

    base = (
        telemetry[KEYS + SENSORS]
        .merge(err_counts, on=KEYS, how="outer")
        .merge(targets, on=KEYS, how="outer")
        .sort_values(KEYS)
        .reset_index(drop=True)
    )
    base[ERROR_COLS] = base[ERROR_COLS].fillna(0)

    # groupby().rolling() 결과는 machine_id 순, 그 안에서 원래 행 순서이므로 base(KEYS 정렬)와 순서가 같다.
    grouped = base.set_index("datetime").groupby("machine_id", sort=True)
    for w in WINDOWS:
        rolling = grouped[SENSORS].rolling(w)  # 센서 값이 없는 행(NaN)은 집계에서 빠진다
        means, stds = rolling.mean(), rolling.std()
        for s in SENSORS:
            base[f"{s}_mean_{w}"] = means[s].to_numpy()
            base[f"{s}_std_{w}"] = stds[s].to_numpy()
    err_sums = grouped[ERROR_COLS].rolling("24h").sum()
    base[ERROR_COLS] = np.rint(err_sums.to_numpy()).astype(int)

    out = targets.merge(base[KEYS + SENSOR_COLS + ERROR_COLS], on=KEYS, how="left")

    # 2) 부품별 마지막 교체 후 경과일: 같은 시각의 교체는 제외 (allow_exact_matches=False)
    out = out.sort_values("datetime").reset_index(drop=True)
    for comp, col in zip(COMPS, DAYS_SINCE_COLS):
        replaced = (
            maint.loc[maint["comp"] == comp, KEYS]
            .drop_duplicates()
            .assign(last_replaced=lambda d: d["datetime"])
            .sort_values("datetime")
        )
        last = pd.merge_asof(
            out[KEYS], replaced, on="datetime", by="machine_id",
            direction="backward", allow_exact_matches=False,
        )
        out[col] = (last["datetime"] - last["last_replaced"]).dt.total_seconds() / 86400

    # 3) 설비 고정 정보. model 범주는 machines 기준으로 고정해 학습·예측에서 같게 만든다.
    out = out.merge(machines[["machine_id", "model", "age"]], on="machine_id", how="left")
    out["model"] = pd.Categorical(out["model"], categories=sorted(machines["model"].unique()))

    return out.sort_values(KEYS).reset_index(drop=True)[KEYS + FEATURE_COLS]


def _add_labels(table: pd.DataFrame, failures: pd.DataFrame) -> pd.DataFrame:
    """t+1시간 ~ t+24시간((t, t+24h]) 안에 고장 나는 부품을 라벨로 붙인다."""
    table = table.sort_values("datetime").reset_index(drop=True)
    for comp in COMPS:
        failed = (
            failures.loc[failures["comp"] == comp, KEYS]
            .drop_duplicates()
            .assign(next_failure=lambda d: d["datetime"])
            .sort_values("datetime")
        )
        nxt = pd.merge_asof(
            table[KEYS], failed, on="datetime", by="machine_id",
            direction="forward", allow_exact_matches=False,
        )
        table[f"y_{comp}"] = ((nxt["next_failure"] - nxt["datetime"]) <= LABEL_HORIZON).astype(int)

    # 같은 구간에 여러 부품이 고장이면 COMPS 순서(motor > drive > pump > bearing)로 우선
    table["label"] = "none"
    for comp in reversed(COMPS):
        table.loc[table[f"y_{comp}"] == 1, "label"] = comp
    return table.sort_values(KEYS).reset_index(drop=True)


def build_training_table() -> pd.DataFrame:
    """telemetry 전체 행에 대해 특징 27개와 라벨을 만들고 data/features.parquet로 저장한다."""
    with sqlite3.connect(DB_PATH) as conn:
        telemetry = _read(conn, "SELECT * FROM telemetry")
        errors = _read(conn, "SELECT * FROM errors")
        maint = _read(conn, "SELECT * FROM maint")
        failures = _read(conn, "SELECT * FROM failures")
        machines = _read(conn, "SELECT * FROM machines")

    table = _compute_features(telemetry, errors, maint, machines, targets=telemetry[KEYS])
    table = _add_labels(table, failures)[KEYS + FEATURE_COLS + LABEL_COLS]

    try:
        table.to_parquet(FEATURES_PATH, index=False)
    except ImportError:
        print("parquet 저장에는 pyarrow가 필요합니다: uv add pyarrow")
        raise
    return table


def build_features(as_of: str) -> pd.DataFrame:
    """as_of 시각의 설비별 특징 27개 (설비 1대 = 1행, 라벨 없음).

    as_of 이후 데이터는 읽지 않는다. telemetry·errors는 최근 24시간 창에 여유를 둔 48시간만 읽는다.
    telemetry·errors는 원본 + 켜진 입력 데이터(store.live_data)를 합쳐 읽는다 (학습용 build_training_table은 원본만).
    """
    from store import live_data  # 예측 때만 필요 (학습 스크립트는 app.db 없이도 돈다)

    as_of_ts = pd.Timestamp(as_of)
    as_of_str = as_of_ts.strftime(DATETIME_FORMAT)
    since_str = (as_of_ts - pd.Timedelta(hours=48)).strftime(DATETIME_FORMAT)

    telemetry = live_data.telemetry_frame(None, since_str, as_of_str)
    errors = live_data.errors_frame(None, since_str, as_of_str)
    with sqlite3.connect(DB_PATH) as conn:
        maint = _read(conn, "SELECT * FROM maint WHERE datetime <= ?", (as_of_str,))
        machines = _read(conn, "SELECT * FROM machines")

    targets = machines[["machine_id"]].assign(datetime=as_of_ts)
    return _compute_features(telemetry, errors, maint, machines, targets)


if __name__ == "__main__":
    print("== build_training_table() ==")
    train = build_training_table()
    print(f"shape: {train.shape}  -> {FEATURES_PATH.relative_to(ROOT_DIR)}")

    print("\nlabel 분포:")
    counts = train["label"].value_counts()
    for label, n in counts.items():
        print(f"  {label:<8} {n:>9,}  ({n / len(train):.3%})")

    n_null = train.isna().sum()
    print(f"\n결측치: 총 {int(n_null.sum()):,}개")
    for col, n in n_null[n_null > 0].items():
        print(f"  {col:<20} {n:,}")

    as_of = "2015-11-12 06:00:00"
    print(f"\n== build_features('{as_of}') vs 학습용 표 같은 시각 ==")
    pred = build_features(as_of)
    ref = train.loc[train["datetime"] == pd.Timestamp(as_of), KEYS + FEATURE_COLS].reset_index(drop=True)

    numeric_cols = [c for c in FEATURE_COLS if c != "model"]
    same_rows = len(pred) == len(ref) == 100 and pred["machine_id"].equals(ref["machine_id"])
    diff = (pred[numeric_cols].astype(float) - ref[numeric_cols].astype(float)).abs()
    both_nan = pred[numeric_cols].isna() & ref[numeric_cols].isna()
    close = (diff <= 1e-6) | both_nan
    same_model = pred["model"].astype(str).equals(ref["model"].astype(str))
    ok = bool(same_rows and close.all().all() and same_model)

    print(f"행 수: 예측용 {len(pred)} / 학습용 {len(ref)}, 최대 절대 오차: {np.nanmax(diff.to_numpy()):.3e}")
    bad_cols = [c for c in numeric_cols if not close[c].all()] + ([] if same_model else ["model"])
    print(f"[{'OK  ' if ok else 'FAIL'}] 특징 {len(FEATURE_COLS)}개 일치 (허용 오차 1e-6)"
          + (f"  불일치 컬럼: {bad_cols}" if bad_cols else ""))
    sys.exit(0 if ok else 1)

# 실행 방법 (프로젝트 루트에서, data/load_db.py 실행 후):
#   uv add pyarrow   # parquet 저장용 (처음 한 번)
#   uv run python ml/features.py
