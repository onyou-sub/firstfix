"""1초 실시간 스트림의 생성기·감지기 (순수 함수, LLM 없음, 시드 고정).

실행: uv run pytest tests/test_stream.py -v
"""

import numpy as np
import pytest

from stream import SENSORS
from stream.baseline import baseline
from stream.detector import detect, machine_state
from stream.generator import MAX_INJECT_SECONDS, next_values

BASE = baseline(45, "2015-11-12 06:00:00")


def run(kind: str | None, sensor: str = "vibration", seconds: int = 120, seed: int = 7, warmup: int = 30):
    """정상 warmup초 뒤 주입. 매초 감지 결과 목록 [(주입 후 경과 초, 설비 상태)]."""
    rng = np.random.default_rng(seed)
    rows, out, last = [], [], {}
    for t in range(warmup + seconds):
        elapsed = t - warmup
        state = {"kind": kind, "sensor": sensor, "elapsed": elapsed, "last": last} if kind and elapsed >= 0 else None
        v = next_values(45, BASE, state, t, rng)
        rows.append(v)
        last = {s: v[s] for s in SENSORS if v[s] is not None} | {s: last.get(s) for s in SENSORS if v[s] is None}
        if elapsed >= 0 or kind is None:
            out.append((elapsed, machine_state(rows, BASE, SENSORS)))
    return out


def first(results, state: str):
    return next((e for e, r in results if r["state"] == state), None)


def test_baseline_shape():
    assert set(BASE) == set(SENSORS)
    assert all(std >= 0.02 * {"volt": 170.8, "rotate": 446.6, "pressure": 100.9, "vibration": 40.4}[s]
               for s, (_, std) in BASE.items())


def test_normal_600s_no_false_alarm():
    results = run(None, seconds=600, warmup=0)
    assert all(r["state"] == "정상" for _, r in results)


def test_spike_no_alarm():
    assert all(r["state"] == "정상" for _, r in run("spike"))


def test_drift_detected_within_40s():
    e = first(run("drift"), "이상")
    assert e is not None and e <= 40, e


def test_step_detected_within_25s():
    e = first(run("step"), "이상")
    assert e is not None and e <= 25, e


def test_stuck_is_sensor_fault():
    results = run("stuck")
    e = first(results, "센서이상")
    assert e is not None and e <= 30 and first(results, "이상") is None


def test_rotate_drift_only_downward():
    down = run("drift", sensor="rotate")
    assert first(down, "이상") is not None and down[-1][1]["sensor"] == "rotate"
    mean, std = BASE["rotate"]
    rng = np.random.default_rng(3)
    up = [{"rotate": mean + 6 * std + rng.normal(0, std)} for _ in range(30)]  # 위 방향은 고장 패턴이 아님
    assert detect(up, BASE, "rotate")["state"] == "정상"
    vib_up = [{"vibration": BASE["vibration"][0] + 6 * BASE["vibration"][1]} for _ in range(30)]
    vib_up[0]["vibration"] += 0.1  # 모두 같은 값이면 센서이상이므로 하나만 다르게
    assert detect(vib_up, BASE, "vibration")["state"] == "이상"


def test_short_window_not_judged():
    rows = [{"vibration": None}] * 19
    assert detect(rows, BASE, "vibration")["state"] == "정상"


def test_drift_error_at_20s_and_injection_ends():
    rng = np.random.default_rng(0)
    errors = [e for e in range(40) if next_values(45, BASE, {"kind": "drift", "sensor": "vibration", "elapsed": e},
                                                     e, rng)["error"]]
    assert errors == [20]
    assert next_values(45, BASE, {"kind": "step", "sensor": "volt", "elapsed": MAX_INJECT_SECONDS}, 0, rng)["done"]
    assert next_values(45, BASE, {"kind": "spike", "sensor": "volt", "elapsed": 2}, 0, rng)["done"]
    with pytest.raises(ValueError):
        next_values(45, BASE, {"kind": "boom", "sensor": "volt", "elapsed": 0}, 0, rng)
