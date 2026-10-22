"""실시간 감지 (순수 함수): 최근 30초 원시값을 설비별 평소값과 통계 비교한다.

detect(window_rows, base, sensor) -> {"state": "정상" | "이상" | "센서이상", "z": 최근 값의 z, "count": 벗어난 초 수}
- 데이터가 20초 미만이면 판단하지 않는다 (정상).
- 최근 30초 값이 모두 같거나 값 없음(None)이 10개 이상 -> 센서이상
- 최근 30초 중 20초 이상이 |z| >= 3 (rotate는 z <= -3, 나머지는 z >= 3) -> 이상
"""

import math

from stream import DOWN_SENSORS, SENSOR_COMP

WINDOW, MIN_ROWS, MIN_COUNT, Z_LIMIT, MAX_NULLS = 30, 20, 20, 3.0, 10
STATES = ("정상", "이상", "센서이상")


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def detect(window_rows: list[dict], base: dict[str, tuple[float, float]], sensor: str) -> dict:
    rows = list(window_rows)[-WINDOW:]
    values = [r.get(sensor) for r in rows]
    present = [float(v) for v in values if not _missing(v)]
    mean, std = base[sensor]
    z = (present[-1] - mean) / std if present else None
    if len(values) < MIN_ROWS:
        return {"state": "정상", "z": z, "count": 0}
    if len(values) - len(present) >= MAX_NULLS or not present or max(present) == min(present):
        return {"state": "센서이상", "z": z, "count": 0}
    zs = [(v - mean) / std for v in present]
    count = sum(x <= -Z_LIMIT for x in zs) if sensor in DOWN_SENSORS else sum(x >= Z_LIMIT for x in zs)
    return {"state": "이상" if count >= MIN_COUNT else "정상", "z": z, "count": int(count)}


def machine_state(window_rows: list[dict], base: dict[str, tuple[float, float]], sensors) -> dict:
    """센서 전체를 보고 설비 상태 하나: 센서이상·이상 중 먼저 걸린 센서 (없으면 정상).
    {"state", "sensor", "comp", "z", "count", "by_sensor": {sensor: detect 결과}}"""
    by = {s: detect(window_rows, base, s) for s in sensors}
    hit = next((s for s in sensors if by[s]["state"] == "센서이상"), None) or \
        next((s for s in sensors if by[s]["state"] == "이상"), None)
    if hit is None:
        return {"state": "정상", "sensor": None, "comp": None, "z": None, "count": 0, "by_sensor": by}
    return {"state": by[hit]["state"], "sensor": hit, "comp": SENSOR_COMP[hit], "z": by[hit]["z"],
            "count": by[hit]["count"], "by_sensor": by}
