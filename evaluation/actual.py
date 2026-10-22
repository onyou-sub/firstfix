"""실제 고장(정답) 조회.

!! 이 모듈은 기준 시각 이후(미래) 데이터를 읽는 프로젝트의 유일한 모듈이다. !!
백테스트·평가에서 예측 결과와 맞춰 보는 용도로만 쓰고,
예측·에이전트 코드(ml/predict.py, ml/features.py 등)에서는 절대 import하지 않는다.
"""

import sqlite3
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "pdm.db"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
HORIZON = pd.Timedelta(hours=24)  # 모델 라벨과 같은 창: (as_of, as_of + 24h]


def actual_failures(as_of: str) -> pd.DataFrame:
    """as_of 초과 ~ as_of+24시간 이하에 실제로 발생한 고장 (machine_id, comp, datetime)."""
    as_of_ts = pd.Timestamp(as_of)
    window = (as_of_ts.strftime(DATETIME_FORMAT), (as_of_ts + HORIZON).strftime(DATETIME_FORMAT))
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT machine_id, comp, datetime FROM failures WHERE datetime > ? AND datetime <= ?"
            " ORDER BY datetime, machine_id, comp",
            conn, params=window,
        )
    df["datetime"] = pd.to_datetime(df["datetime"], format=DATETIME_FORMAT)
    return df
