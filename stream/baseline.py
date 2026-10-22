"""설비별 평소값: 원본 telemetry에서 기준 시각 이전 30일의 센서별 평균·표준편차 (입력 데이터 제외).

- std가 너무 작으면 1년 평균의 2%를 하한으로 쓴다 (값이 거의 일정한 센서에서 z가 폭주하지 않게).
- 기준 시각마다 한 번 계산해 프로세스 메모리에 캐시한다 (clear_cache로 비움).
"""

import sqlite3

import pandas as pd

from store.live_data import PDM_PATH
from stream import SENSORS

DAYS = 30
SENSOR_MEAN = {"volt": 170.8, "rotate": 446.6, "pressure": 100.9, "vibration": 40.4}  # tools.data_tools와 같은 값
STD_FLOOR_PCT = 0.02
_CACHE: dict[tuple[int, str], dict[str, tuple[float, float]]] = {}


def _ts(value) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def baseline(machine_id: int, as_of: str) -> dict[str, tuple[float, float]]:
    """{sensor: (mean, std)} — (as_of - 30일, as_of] 원본 telemetry."""
    key = (int(machine_id), _ts(as_of))
    if key in _CACHE:
        return _CACHE[key]
    end = pd.Timestamp(as_of)
    start = end - pd.Timedelta(days=DAYS)
    cols = ", ".join(f"AVG({s}), AVG({s} * {s})" for s in SENSORS)
    with sqlite3.connect(f"{PDM_PATH.as_uri()}?mode=ro", uri=True) as conn:
        row = conn.execute(f"SELECT COUNT(*), {cols} FROM telemetry WHERE machine_id = ? AND datetime > ? AND datetime <= ?",
                           (int(machine_id), _ts(start), _ts(end))).fetchone()
    out = {}
    for i, s in enumerate(SENSORS):
        mean, sq = row[1 + 2 * i], row[2 + 2 * i]
        if not row[0] or mean is None:  # 기록이 없으면 1년 평균과 하한 표준편차
            mean, std = SENSOR_MEAN[s], SENSOR_MEAN[s] * STD_FLOOR_PCT
        else:
            std = max(sq - mean * mean, 0.0) ** 0.5
        out[s] = (float(mean), float(max(std, SENSOR_MEAN[s] * STD_FLOOR_PCT)))
    _CACHE[key] = out
    return out


def clear_cache() -> None:
    _CACHE.clear()
