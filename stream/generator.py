"""1초 센서 값 생성 (순수 함수, 시계를 읽지 않는다 — 경과 시간은 inject_state["elapsed"] 초로 받는다).

- 정상: mean + N(0, STREAM_NOISE·std)  (std는 시간 단위 흩어짐이라 1초 잡음으로는 너무 커서 0.3배)
- 이상 주입 4종 (방향: rotate는 아래, 나머지는 위)
  · drift (서서히 상승): 30초 동안 mean -> mean + 6·std로 선형 변화 후 유지, 20초 시점에 관련 에러 1건
  · spike (갑자기 튐): 2초 동안 mean + 8·std, 이후 정상 (주입 끝)
  · step  (계단): 즉시 mean + 6·std로 바뀌어 유지
  · stuck (센서 멈춤): 마지막 값을 그대로 반복, 그중 3초에 1번은 값 없음(None)
- recover (정비 후 복귀): 긴급 작업 완료 보고가 오면 생성기가 넣는다. 주입 순간 값(from)에서 10초 동안 평소값으로
  내려온다. 이상 주입이 아니므로 injected는 None (주황 표시 없음)
- 유지되는 주입은 [정상 복귀] 명령이나 180초 경과 시 끝난다 (done=True를 돌려주면 생성기 루프가 지운다).
"""

import numpy as np

from stream import COMP_ERRORS, DOWN_SENSORS, SENSOR_COMP, SENSORS

KINDS = ("drift", "spike", "step", "stuck")
KIND_KO = {"drift": "서서히 상승", "spike": "갑자기 튐", "step": "계단 변화", "stuck": "센서 멈춤"}
DRIFT_SECONDS, DRIFT_SIGMA, DRIFT_ERROR_AT = 30, 6.0, 20
SPIKE_SECONDS, SPIKE_SIGMA = 2, 8.0
STEP_SIGMA = 6.0
STUCK_NULL_EVERY = 3
MAX_INJECT_SECONDS = 180
STREAM_NOISE = 0.3  # 1초 잡음 = 0.3·std
RECOVER_SECONDS = 10  # 정비 후 평소값으로 내려오는 시간


def _sign(sensor: str) -> float:
    return -1.0 if sensor in DOWN_SENSORS else 1.0


def next_values(machine_id: int, base: dict[str, tuple[float, float]], inject_state: dict | None, tick: int,
                rng: np.random.Generator) -> dict:
    """1초 값 1행. inject_state = {"kind", "sensor", "elapsed"(주입 후 경과 초, 0부터), "last"({sensor: 직전 값})} 또는 None.

    반환: {volt, rotate, pressure, vibration, "injected": 주입 종류 또는 None, "injected_sensor": 주입 센서 또는 None,
           "error": 에러 id 또는 None, "done": 주입이 끝났으면 True}
    """
    out = {s: float(base[s][0] + rng.normal(0.0, STREAM_NOISE * base[s][1])) for s in SENSORS}
    out.update(injected=None, injected_sensor=None, error=None, done=False)
    if not inject_state:
        return out
    kind, sensor, elapsed = inject_state["kind"], inject_state["sensor"], int(inject_state["elapsed"])
    if elapsed >= MAX_INJECT_SECONDS:
        out["done"] = True
        return out
    mean, std = base[sensor]
    sign = _sign(sensor)
    if kind == "recover":  # 정비 완료 -> 평소값으로 복귀 (이상 표시 없음)
        start = inject_state.get("from")
        if elapsed >= RECOVER_SECONDS or start is None:
            out["done"] = True
            return out
        k = elapsed / RECOVER_SECONDS
        out[sensor] = float(start + (mean - start) * k + rng.normal(0.0, STREAM_NOISE * std))
        return out
    if kind == "drift":
        k = min(elapsed / DRIFT_SECONDS, 1.0)
        out[sensor] = float(mean + sign * DRIFT_SIGMA * std * k + rng.normal(0.0, STREAM_NOISE * std))
        if elapsed == DRIFT_ERROR_AT:
            out["error"] = COMP_ERRORS[SENSOR_COMP[sensor]]
    elif kind == "spike":
        if elapsed >= SPIKE_SECONDS:
            out["done"] = True
            return out
        out[sensor] = float(mean + sign * SPIKE_SIGMA * std)
    elif kind == "step":
        out[sensor] = float(mean + sign * STEP_SIGMA * std + rng.normal(0.0, STREAM_NOISE * std))
    elif kind == "stuck":
        last = (inject_state.get("last") or {}).get(sensor)
        out[sensor] = None if elapsed % STUCK_NULL_EVERY == STUCK_NULL_EVERY - 1 else float(mean if last is None else last)
    else:
        raise ValueError(f"주입 종류는 {KINDS} 중 하나여야 합니다: {kind!r}")
    out["injected"] = kind
    out["injected_sensor"] = sensor
    return out
