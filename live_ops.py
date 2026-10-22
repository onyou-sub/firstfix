"""입력 데이터 → 즉시 판단 → 긴급 점검 (화면은 services.py를 통해서만 부른다. Streamlit 코드 없음).

1단계 입력: add_scenario / add_manual / add_csv / list_batches / deactivate_batch / clear_live_data
  - 입력 데이터는 app.db live_* 테이블에만 저장한다 (원본 pdm.db는 읽기만).
  - 모든 입력 함수는 {"batch_id", "machine_ids", "rows", "rejected", "alerts"}를 돌려준다.
    입력이 잘못되면 예외 대신 "error"(한국어 문장)를 담아 돌려주고 아무것도 저장하지 않는다.
2단계 판단: evaluate_machine (입력 직후 설비마다 자동 호출, 결과는 "alerts") / open_alerts / alert_history
3단계 긴급 점검: handle_alert (새 경고마다 백그라운드) -> 조사 에이전트 -> dispatch_urgent (UWO, 승인 없이 자동 배정)
  - 긴급 "점검"만 자동 배정한다. 부품 교체 작업 지시·발주는 만들지 않는다 (기존처럼 관리자 승인 후에만).
4단계 화면용: alert_progress / scenario_preview / urgent_orders / live_version
"""

import io
import json
import os
import sqlite3
import threading
import zlib
from datetime import datetime, timedelta
from functools import cache

import numpy as np
import pandas as pd

from config import COMP_LABEL_KO
from store import app_db, live_data

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SENSORS = live_data.SENSORS
SENSOR_MEAN = {"volt": 170.8, "rotate": 446.6, "pressure": 100.9, "vibration": 40.4}  # tools.data_tools와 같은 값
COMP_SENSOR = {"motor": "volt", "drive": "rotate", "pump": "pressure", "bearing": "vibration"}
SENSOR_COMP = {s: c for c, s in COMP_SENSOR.items()}
COMP_ERRORS = {"motor": ["overvoltage"], "drive": ["low_rpm", "torque"], "pump": ["pressure"], "bearing": ["vibration"]}
ERROR_IDS = ["overvoltage", "low_rpm", "torque", "pressure", "vibration"]
# 시나리오: 부품, 짝 센서, 1년 평균 대비 목표 변화율.
# 요청서 값(+25%, 회전수 -15%)으로는 18~24시간 안에서 모델(24시간 내내 높은 패턴을 학습)도, 규칙(+30%/-20%)도
# 반응하지 않아 +35%, -25%로 올렸다. 마지막 구간이 규칙 경고 기준을 넘는다.
SCENARIOS = {
    "bearing_vibration": ("bearing", "vibration", 0.35),
    "motor_voltage": ("motor", "volt", 0.35),
    "pump_pressure": ("pump", "pressure", 0.35),
    "drive_rotation": ("drive", "rotate", -0.25),
}
SCENARIO_KO = {"bearing_vibration": "베어링 진동 상승", "motor_voltage": "모터 전압 상승",
               "pump_pressure": "펌프 압력 상승", "drive_rotation": "구동부 회전수 저하"}
KIND_KO = {"scenario": "시나리오", "manual": "직접 입력", "csv": "CSV", "stream": "실시간 스트림"}
NOISE_PCT = 0.01  # 시나리오 잡음: 1년 평균의 1%
ERROR_LAST_HOURS = 6
CSV_RANGE = (0.5, 1.5)  # CSV 센서 값 허용 범위: 원본 최소×0.5 ~ 최대×1.5
TELEMETRY_CSV_COLS = ["datetime", "machine_id", *SENSORS]
ERROR_CSV_COLS = ["datetime", "machine_id", "error_id"]


def _ts(value) -> str:
    return pd.Timestamp(value).strftime(DATETIME_FORMAT)


def _result(batch_id=None, machine_ids=(), rows=0, rejected=(), error: str | None = None) -> dict:
    out = {"batch_id": batch_id, "machine_ids": sorted({int(m) for m in machine_ids}), "rows": int(rows),
           "rejected": list(rejected)}
    if error:
        out["error"] = error
    return out


def _machine_error(machine_id) -> str | None:
    if isinstance(machine_id, bool) or not isinstance(machine_id, (int, np.integer)) or not 1 <= machine_id <= 100:
        return f"설비 번호는 1~100 사이 정수여야 합니다 (받은 값: {machine_id!r})"
    return None


def _raw_hours(machine_id: int, times: list[pd.Timestamp]) -> pd.DataFrame:
    """원본 telemetry를 times 시각마다 한 행으로 (원본에 없는 시각은 1년 평균으로 채움). index = datetime"""
    raw = live_data._read_raw("telemetry", ["datetime", *SENSORS], [machine_id],
                              _ts(times[0] - pd.Timedelta(seconds=1)), _ts(times[-1]))
    raw["datetime"] = pd.to_datetime(raw["datetime"], format=DATETIME_FORMAT)
    frame = raw.set_index("datetime").reindex(times)
    return frame.fillna(SENSOR_MEAN)


def _next_batch_id(conn, prefix: str) -> str:
    n = conn.execute("SELECT COUNT(*) FROM live_batches WHERE batch_id LIKE ?", (f"{prefix}-%",)).fetchone()[0]
    while conn.execute("SELECT 1 FROM live_batches WHERE batch_id = ?", (f"{prefix}-{n + 1:02d}",)).fetchone():
        n += 1
    return f"{prefix}-{n + 1:02d}"


def _save(prefix: str, machine_id: int | None, kind: str, description: str, telemetry: pd.DataFrame | None,
          errors: pd.DataFrame | None, created_by: str | None) -> str:
    """배치 1개와 행들을 한 트랜잭션으로 저장하고 batch_id를 돌려준다."""
    created = app_db.now()
    frames = [f for f in (telemetry, errors) if f is not None and len(f)]
    times = pd.concat([f["datetime"] for f in frames]) if frames else pd.Series(dtype=str)
    with app_db._lock, app_db.connect() as conn:
        batch_id = _next_batch_id(conn, prefix)
        conn.execute('INSERT INTO live_batches (batch_id, machine_id, kind, description, start, "end", active,'
                     " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                     (batch_id, machine_id, kind, description, times.min() if len(times) else None,
                      times.max() if len(times) else None, created_by, created))
        if telemetry is not None and len(telemetry):
            conn.executemany(
                "INSERT INTO live_telemetry (batch_id, datetime, machine_id, volt, rotate, pressure, vibration,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(batch_id, r.datetime, int(r.machine_id), *(float(getattr(r, s)) for s in SENSORS), created)
                 for r in telemetry.itertuples(index=False)])
        if errors is not None and len(errors):
            conn.executemany(
                "INSERT INTO live_errors (batch_id, datetime, machine_id, error_id, created_at) VALUES (?, ?, ?, ?, ?)",
                [(batch_id, r.datetime, int(r.machine_id), r.error_id, created) for r in errors.itertuples(index=False)])
    return batch_id


# ---------------------------------------------------------------- 1단계: 입력 함수
def scenario_frames(machine_id: int, kind: str, as_of: str, hours: int = 18) -> tuple[pd.DataFrame, pd.DataFrame]:
    """시나리오 행을 만든다 (저장하지 않음, 미리보기에도 쓴다). 같은 (설비, 기준 시각)이면 항상 같은 값.

    (as_of - hours, as_of] 시간별 행: 나머지 센서는 원본 복사. 짝 센서는 처음 1/3은 원본 그대로
    -> 가운데 1/3 동안 1년 평균 대비 목표(SCENARIOS)만큼 서서히 올라가고 -> 마지막 1/3은 그 수준 유지.
    원본 값에 차이를 더하는 방식이라 원래의 흔들림(표준편차)은 그대로 남는다 (+ 약간의 잡음).
    마지막 6시간 안에 관련 에러 1~2건.
    """
    comp, sensor, change = SCENARIOS[kind]
    as_of_ts = pd.Timestamp(as_of)
    times = [as_of_ts - pd.Timedelta(hours=hours - 1 - i) for i in range(hours)]
    rng = np.random.default_rng(zlib.crc32(f"{machine_id}|{_ts(as_of)}".encode()))
    frame = _raw_hours(machine_id, times)
    flat = hours // 3
    rise = max(hours // 3, 1)
    ramp = np.clip((np.arange(hours) - flat + 1) / rise, 0, 1)
    noise = rng.normal(0, SENSOR_MEAN[sensor] * NOISE_PCT, hours) * (ramp > 0)
    orig = frame[sensor].to_numpy()
    # 더할 차이: 마지막 3시간 원본 평균이 정확히 목표에 닿도록 (원본 흔들림 때문에 규칙 판정이 갈리지 않게)
    delta = SENSOR_MEAN[sensor] * (1 + change) - orig[-RULE_HOURS:].mean()
    frame[sensor] = orig + delta * ramp + noise
    telemetry = frame.reset_index(names="datetime").assign(machine_id=machine_id)
    telemetry["datetime"] = telemetry["datetime"].map(_ts)

    last = times[-ERROR_LAST_HOURS:]
    n_err = int(rng.integers(1, 3))
    picks = sorted(rng.choice(len(last), size=min(n_err, len(last)), replace=False))
    errors = pd.DataFrame({"datetime": [_ts(last[i]) for i in picks], "machine_id": machine_id,
                           "error_id": [str(rng.choice(COMP_ERRORS[comp])) for _ in picks]})
    return telemetry[["datetime", "machine_id", *SENSORS]], errors


def add_scenario(machine_id: int, kind: str, as_of: str, hours: int = 18, created_by: str | None = None) -> dict:
    if err := _machine_error(machine_id):
        return _result(error=err)
    if kind not in SCENARIOS:
        return _result(error=f"시나리오는 {', '.join(SCENARIOS)} 중 하나여야 합니다 (받은 값: {kind!r})")
    if isinstance(hours, bool) or not isinstance(hours, int) or not 3 <= hours <= 48:
        return _result(error=f"기간은 3~48시간 사이 정수여야 합니다 (받은 값: {hours!r})")
    telemetry, errors = scenario_frames(machine_id, kind, as_of, hours)
    as_of_ts = pd.Timestamp(as_of)
    batch_id = _save(f"B-{as_of_ts:%m%d}-{machine_id:03d}", machine_id, "scenario",
                     f"{SCENARIO_KO[kind]} {hours}시간", telemetry, errors, created_by)
    return _evaluate_input(_result(batch_id, [machine_id], len(telemetry) + len(errors)), as_of)


def add_manual(machine_id: int, sensor: str, value: float, start: str, end: str, created_by: str | None = None,
               as_of: str | None = None) -> dict:
    """[start, end] 안의 정시마다 해당 센서만 value로, 나머지 센서는 원본 복사. as_of가 오면 end <= as_of만 허용."""
    if err := _machine_error(machine_id):
        return _result(error=err)
    if sensor not in SENSORS:
        return _result(error=f"센서는 {', '.join(SENSORS)} 중 하나여야 합니다 (받은 값: {sensor!r})")
    try:
        value = float(value)
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    except (TypeError, ValueError):
        return _result(error="값은 숫자, 시작·끝은 'YYYY-MM-DD HH:MM' 형식이어야 합니다.")
    if not np.isfinite(value) or value <= 0:
        return _result(error=f"값은 0보다 큰 숫자여야 합니다 (받은 값: {value!r})")
    if end_ts < start_ts:
        return _result(error="끝 시각이 시작 시각보다 빠릅니다.")
    if as_of is not None and end_ts > pd.Timestamp(as_of):
        return _result(error=f"기준 시각({_ts(as_of)}) 이후 구간에는 넣을 수 없습니다.")
    times = list(pd.date_range(start_ts.ceil("h"), end_ts.floor("h"), freq="h"))
    if not times:
        return _result(error="구간 안에 정시(HH:00)가 없습니다. 1시간 이상으로 잡으세요.")
    frame = _raw_hours(machine_id, times)
    frame[sensor] = value
    telemetry = frame.reset_index(names="datetime").assign(machine_id=machine_id)
    telemetry["datetime"] = telemetry["datetime"].map(_ts)
    batch_id = _save(f"B-{end_ts:%m%d}-{machine_id:03d}", machine_id, "manual",
                     f"{sensor} = {value:g} ({_ts(times[0])[5:16]} ~ {_ts(times[-1])[5:16]})", telemetry, None, created_by)
    return _evaluate_input(_result(batch_id, [machine_id], len(telemetry)), as_of or _ts(end_ts))


@cache
def _sensor_bounds() -> dict[str, tuple[float, float]]:
    """CSV 검사용 센서별 허용 범위 (원본 최소×0.5 ~ 최대×1.5)."""
    with sqlite3.connect(f"{live_data.PDM_PATH.as_uri()}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT " + ", ".join(f"MIN({s}), MAX({s})" for s in SENSORS) + " FROM telemetry").fetchone()
    return {s: (row[2 * i] * CSV_RANGE[0], row[2 * i + 1] * CSV_RANGE[1]) for i, s in enumerate(SENSORS)}


def csv_example(kind: str = "telemetry") -> str:
    """화면의 [예시 CSV 내려받기]용."""
    if kind == "errors":
        return "datetime,machine_id,error_id\n2015-11-12 04:00:00,45,vibration\n"
    return ("datetime,machine_id,volt,rotate,pressure,vibration\n"
            "2015-11-12 05:00:00,45,170.2,446.0,100.5,52.1\n2015-11-12 06:00:00,45,171.0,445.3,101.2,55.8\n")


def add_csv(file_bytes: bytes, as_of: str, created_by: str | None = None) -> dict:
    """센서 CSV(datetime, machine_id, volt, rotate, pressure, vibration) 또는 에러 CSV(datetime, machine_id, error_id).
    한 행이라도 틀리면 아무것도 저장하지 않고, 틀린 행 번호(머리글 = 1행)와 이유를 rejected로 돌려준다."""
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, skipinitialspace=True)
    except Exception as e:  # 형식이 깨진 파일
        return _result(error=f"CSV를 읽을 수 없습니다: {type(e).__name__}")
    df.columns = [str(c).strip() for c in df.columns]
    is_errors = "error_id" in df.columns
    need = ERROR_CSV_COLS if is_errors else TELEMETRY_CSV_COLS
    if missing := [c for c in need if c not in df.columns]:
        return _result(error=f"필요한 열이 없습니다: {', '.join(missing)} (필요: {', '.join(need)})")
    if df.empty:
        return _result(error="데이터 행이 없습니다.")

    as_of_ts, bounds = pd.Timestamp(as_of), _sensor_bounds()
    rows, rejected, seen = [], [], set()
    for i, r in enumerate(df[need].itertuples(index=False), start=2):
        reasons = []
        try:
            ts = pd.Timestamp(str(r.datetime).strip())
        except ValueError:
            ts = None
            reasons.append(f"datetime 형식 오류({r.datetime!r})")
        try:
            m = int(str(r.machine_id).strip())
            if not 1 <= m <= 100:
                raise ValueError
        except ValueError:
            m = None
            reasons.append(f"machine_id는 1~100 정수({r.machine_id!r})")
        if ts is not None and ts > as_of_ts:
            reasons.append(f"기준 시각({_ts(as_of_ts)}) 이후")
        row = {"datetime": _ts(ts) if ts is not None else None, "machine_id": m}
        if is_errors:
            eid = str(r.error_id).strip()
            if eid not in ERROR_IDS:
                reasons.append(f"error_id는 {', '.join(ERROR_IDS)} 중 하나({r.error_id!r})")
            row["error_id"] = eid
        else:
            for s in SENSORS:
                try:
                    v = float(getattr(r, s))
                    if not np.isfinite(v):
                        raise ValueError
                except (TypeError, ValueError):
                    reasons.append(f"{s} 값이 숫자가 아님")
                    continue
                lo, hi = bounds[s]
                if not lo <= v <= hi:
                    reasons.append(f"{s} {v:g}가 허용 범위 {lo:.1f}~{hi:.1f} 밖")
                row[s] = v
            key = (row["machine_id"], row["datetime"])
            if not reasons and key in seen:
                reasons.append("같은 설비·시각이 이미 위에 있음")
            seen.add(key)
        if reasons:
            rejected.append({"row": i, "reason": ", ".join(reasons)})
        else:
            rows.append(row)
    if rejected:
        return _result(rows=0, rejected=rejected)

    frame = pd.DataFrame(rows)
    ids = sorted(frame["machine_id"].unique())
    single = int(ids[0]) if len(ids) == 1 else None
    prefix = f"B-{as_of_ts:%m%d}-" + (f"{single:03d}" if single else "CSV")
    desc = f"CSV {'에러' if is_errors else '센서'} {len(frame)}행"
    batch_id = _save(prefix, single, "csv", desc, None if is_errors else frame, frame if is_errors else None, created_by)
    return _evaluate_input(_result(batch_id, ids, len(frame)), as_of)


def list_batches() -> list[dict]:
    """입력 배치 목록 (최신순). 행 수(rows)와 종류 한글(kind_ko) 포함."""
    rows = app_db._rows(
        "SELECT b.*, (SELECT COUNT(*) FROM live_telemetry t WHERE t.batch_id = b.batch_id)"
        " + (SELECT COUNT(*) FROM live_errors e WHERE e.batch_id = b.batch_id) AS rows"
        " FROM live_batches b ORDER BY b.created_at DESC, b.batch_id DESC")
    return [{**r, "kind_ko": KIND_KO.get(r["kind"], r["kind"])} for r in rows]


def deactivate_batch(batch_id: str) -> dict:
    """배치를 끈다 (행은 남기고 예측·조사에서만 빠짐). {"batch_id", "ok"} 또는 {"error"}"""
    with app_db._lock, app_db.connect() as conn:
        n = conn.execute("UPDATE live_batches SET active = 0 WHERE batch_id = ?", (batch_id,)).rowcount
    return {"batch_id": batch_id, "ok": True} if n else {"batch_id": batch_id, "ok": False,
                                                          "error": f"없는 배치입니다: {batch_id}"}


# ---------------------------------------------------------------- 2단계: 즉시 판단과 경고 (LLM 없음)
LEVELS = ("경고", "주의", "사람 확인", "정상")
LEVEL_RANK = {"정상": 0, "주의": 1, "사람 확인": 2, "경고": 2}
CLOSED = ("점검 완료", "종료")
CAUTION_PROB = 0.3
RULE_HOURS = 3
RULE_UP, RULE_DOWN = 0.30, -0.20  # 짝 센서 최근 3시간 평균이 1년 평균 대비 +30% 이상 (회전수는 -20% 이하)
RECENT_REPLACE_DAYS = 7


def _recent_replaced(machine_id: int, comp: str, as_of: str) -> float | None:
    """comp를 as_of 기준 7일 안에 교체했으면 경과일, 아니면 None (원본 maint + 작업자 보고 maint_log)."""
    from tools.data_tools import _maint_rows

    rows = _maint_rows(machine_id, as_of)
    rows = rows[rows["comp"] == comp]
    if rows.empty:
        return None
    days = (pd.Timestamp(as_of) - rows["datetime"].max()).total_seconds() / 86400
    return round(days, 1) if days <= RECENT_REPLACE_DAYS else None


def _rule_hit(machine_id: int, as_of: str) -> tuple[str, str, float] | None:
    """규칙 경고: 센서별 최근 3시간 평균의 1년 평균 대비 변화. 걸리면 (센서, 짝 부품, 변화율) 중 가장 큰 것."""
    from tools.data_tools import _sensor_rows

    hits = []
    for sensor in SENSORS:
        values = _sensor_rows(machine_id, sensor, RULE_HOURS, as_of)["value"]
        if values.empty:
            continue
        change = values.mean() / SENSOR_MEAN[sensor] - 1
        if (change <= RULE_DOWN) if sensor == "rotate" else (change >= RULE_UP):
            hits.append((sensor, SENSOR_COMP[sensor], change))
    return max(hits, key=lambda h: abs(h[2])) if hits else None


def _open_alert(machine_id: int, as_of: str | None = None) -> dict | None:
    """닫히지 않은 최신 경고. as_of를 주면 그 기준 날짜의 경고만 본다 (이전 날짜에 남은 경고가 오늘을 막지 않게)."""
    where, params = "machine_id = ?", [int(machine_id)]
    if as_of:
        where += " AND substr(as_of, 1, 10) = ?"
        params.append(_ts(as_of)[:10])
    rows = app_db._rows(f"SELECT * FROM alerts WHERE {where} AND status NOT IN ({', '.join('?' * len(CLOSED))})"
                        " ORDER BY created_at DESC LIMIT 1", (*params, *CLOSED))
    return rows[0] if rows else None


STALE_STATUSES = ("열림", "대기")  # 배정 전 상태. 이미 배정된 경고(점검 배정 등)는 날짜가 바뀌어도 남긴다


def close_stale_alerts(as_of: str) -> int:
    """기준 날짜 이전에 배정 전 상태로 남은 경고를 "종료"로 닫는다. 닫은 건수를 돌려준다."""
    day = _ts(as_of)[:10]
    with app_db._lock, app_db.connect() as conn:
        return conn.execute(
            "UPDATE alerts SET status = '종료', investigating = 0, message = message || ' · 기준 날짜 변경으로 종료',"
            f" updated_at = ? WHERE substr(as_of, 1, 10) < ? AND status IN ({', '.join('?' * len(STALE_STATUSES))})",
            (app_db.now(), day, *STALE_STATUSES)).rowcount


STUCK_SECONDS = 30  # 생긴 지 이만큼 지나도 배정 전 상태면 멈춘 것으로 본다 (방금 생긴 경고는 처리 중일 수 있다)


def retry_stuck_alerts(as_of: str) -> list[str]:
    """기준 날짜의 경고 중 배정 전 상태로 STUCK_SECONDS 넘게 멈춘 것을 다시 처리한다. 다시 처리한 alert_id 목록.
    """
    since = (pd.Timestamp(app_db.now()) - pd.Timedelta(seconds=STUCK_SECONDS)).isoformat(timespec="seconds")
    rows = app_db._rows(
        f"SELECT alert_id FROM alerts WHERE substr(as_of, 1, 10) = ? AND level IN ({', '.join('?' * len(DISPATCH_LEVELS))})"
        f" AND status IN ({', '.join('?' * len(STALE_STATUSES))}) AND created_at < ? ORDER BY created_at",
        (_ts(as_of)[:10], *DISPATCH_LEVELS, *STALE_STATUSES, since))
    ids = [r["alert_id"] for r in rows]
    for alert_id in ids:
        handle_alert(alert_id)
    return ids


def _alert_out(a: dict, is_new: bool) -> dict:
    return {"alert_id": a["alert_id"], "machine_id": a["machine_id"], "comp": a["comp"], "prob": a["prob"],
            "level": a["level"], "source": a["source"], "status": a["status"], "message": a["message"],
            "is_new": is_new}


def judge(machine_id: int, as_of: str, stream_hint: dict | None = None) -> dict:
    """판정만 (저장 없음). {"level", "source", "comp", "prob", "message"}

    순서: (실시간 힌트 또는 모델 고위험 / 센서 규칙 / 확률 0.3 이상)으로 이상 징후를 찾고,
    징후가 있는데 그 부품을 최근 7일 안에 교체했으면 "사람 확인"(센서·설치 확인)으로 바꾼다.
    """
    from ml.predict import predict_df

    pred = predict_df(as_of).set_index("machine_id").loc[machine_id]
    comp, prob = str(pred["top_comp"]), float(pred["top_prob"])
    if stream_hint:
        comp = stream_hint.get("comp") or SENSOR_COMP[stream_hint["sensor"]]
        if stream_hint.get("state") == "센서이상":
            return {"level": "사람 확인", "source": "실시간", "comp": comp, "prob": prob,
                    "message": f"센서 값 멈춤/누락 ({stream_hint['sensor']})"}
        level, source = "경고", "실시간"
        message = f"{stream_hint['sensor']} 실시간 이상 (z {float(stream_hint.get('z') or 0):+.1f})"
    elif bool(pred["high_risk"]):
        level, source, message = "경고", "모델", f"모델 고위험 {comp} 확률 {prob:.2f}"
    elif hit := _rule_hit(machine_id, as_of):
        sensor, comp, change = hit
        level, source = "경고", "규칙"
        message = f"{sensor} 최근 {RULE_HOURS}시간 평균 1년 평균 대비 {change * 100:+.0f}%"
    elif prob >= CAUTION_PROB:
        level, source, message = "주의", "모델", f"{comp} 확률 {prob:.2f}"
    else:
        return {"level": "정상", "source": "모델", "comp": comp, "prob": prob, "message": f"{comp} 확률 {prob:.2f}"}
    if (days := _recent_replaced(machine_id, comp, as_of)) is not None:
        level, message = "사람 확인", f"{message} · {comp} {days}일 전 교체 → 센서·설치 확인"
    return {"level": level, "source": source, "comp": comp, "prob": prob, "message": message}


def evaluate_machine(machine_id: int, as_of: str, batch_id: str | None = None,
                     stream_hint: dict | None = None) -> dict:
    """설비 1대를 바로 판단해 경고를 남긴다 (LLM 없음, 관리자 알림 없음).

    반환: {"alert_id", "machine_id", "comp", "prob", "level", "source", "status", "message", "is_new"}
    - 정상이면 저장하지 않는다 (alert_id None).
    - 같은 설비에 닫히지 않은 경고가 있으면 새로 만들지 않고 그 경고를 돌려준다 (is_new False).
      단, 열린 것이 "주의"이고 이번 판정이 경고·사람 확인이면 그 경고를 올려서 새 경고처럼 처리한다 (is_new True).
    stream_hint={"sensor", "comp", "state"(이상/센서이상), "z"}: 실시간 감지기에서 온 판단 (5단계).
    """
    if err := _machine_error(machine_id):
        return {"alert_id": None, "machine_id": machine_id, "level": "정상", "is_new": False, "error": err}
    as_of = _ts(as_of)
    j = judge(machine_id, as_of, stream_hint)
    if j["level"] == "정상":
        return {"alert_id": None, "machine_id": machine_id, **j, "status": None, "is_new": False}
    now = app_db.now()
    with app_db._lock, app_db.connect() as conn:
        cur = conn.execute(f"SELECT * FROM alerts WHERE machine_id = ? AND substr(as_of, 1, 10) = ?"
                           f" AND status NOT IN ({', '.join('?' * len(CLOSED))})"
                           " ORDER BY created_at DESC LIMIT 1", (machine_id, as_of[:10], *CLOSED)).fetchone()
        if cur is not None:
            cur = dict(cur)
            if not (cur["level"] == "주의" and LEVEL_RANK[j["level"]] > LEVEL_RANK["주의"]):
                return _alert_out(cur, is_new=False)
            conn.execute("UPDATE alerts SET level = ?, source = ?, comp = ?, prob = ?, message = ?, batch_id = ?,"
                         " as_of = ?, status = '열림', updated_at = ? WHERE alert_id = ?",
                         (j["level"], j["source"], j["comp"], j["prob"], j["message"], batch_id or cur["batch_id"],
                          as_of, now, cur["alert_id"]))
            return _alert_out({**cur, **j, "status": "열림"}, is_new=True)
        prefix = f"A-{pd.Timestamp(as_of):%m%d}-{machine_id:03d}"
        n = conn.execute("SELECT COUNT(*) FROM alerts WHERE alert_id LIKE ?", (f"{prefix}-%",)).fetchone()[0]
        alert = {"alert_id": f"{prefix}-{n + 1:02d}", "as_of": as_of, "machine_id": machine_id, **j,
                 "batch_id": batch_id, "status": "열림"}
        conn.execute("INSERT INTO alerts (alert_id, as_of, machine_id, comp, prob, level, source, batch_id, status,"
                     " message, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (alert["alert_id"], as_of, machine_id, j["comp"], j["prob"], j["level"], j["source"], batch_id,
                      "열림", j["message"], now, now))
    return _alert_out(alert, is_new=True)


def _evaluate_input(result: dict, as_of: str) -> dict:
    """입력 함수 결과에 설비별 즉시 판단을 붙인다 (저장에 실패했으면 그대로)."""
    if result.get("batch_id"):
        result["alerts"] = [evaluate_machine(m, as_of, result["batch_id"]) for m in result["machine_ids"]]
        on_new_alerts(result["alerts"])
    else:
        result["alerts"] = []
    return result


def on_new_alerts(alerts: list[dict]) -> None:
    """새 경고(경고·사람 확인)와 대기 중이던 경고를 자동 조사·긴급 점검 배정으로 넘긴다 (화면을 막지 않게 백그라운드)."""
    ids = [a["alert_id"] for a in alerts if a.get("is_new") and a.get("level") in DISPATCH_LEVELS]
    ids += [r["alert_id"] for r in app_db._rows("SELECT alert_id FROM alerts WHERE status = '대기' ORDER BY created_at")
            if r["alert_id"] not in ids]
    for alert_id in ids:
        if RUN_ASYNC:
            threading.Thread(target=handle_alert, args=(alert_id,), daemon=True, name=f"alert-{alert_id}").start()
        else:
            handle_alert(alert_id)


# ---------------------------------------------------------------- 3단계: 자동 조사와 긴급 점검 배정
DISPATCH_LEVELS = ("경고", "사람 확인")  # "주의"는 기록만 (배정하지 않음)
RUN_ASYNC = True  # 테스트에서 False로 바꾸면 handle_alert를 바로 실행
AUTO_APPROVER = "auto(긴급 점검)"
TASK_KO = {"urgent_check": "긴급 점검", "sensor_check": "센서 점검"}
MAX_INVESTIGATIONS_PER_HOUR = 10
# 조사를 생략하고 배정할 때 쓰는 기본 점검 항목
URGENT_CHECKLIST = {comp: ["설비 정지·잠금 확인", f"{sensor} 센서 값 현장 측정", f"{COMP_LABEL_KO[comp]} 외관·소음·발열 점검",
                           "재가동 후 센서 값 확인"] for comp, sensor in COMP_SENSOR.items()}
SENSOR_CHECKLIST = ["센서 배선·커넥터 확인", "센서 고정·설치 상태 확인", "휴대 측정기로 값 비교", "필요 시 센서 교정·교체"]
_locks_guard = threading.Lock()
_alert_locks: dict[str, threading.Lock] = {}


def _investigate(machine_id: int, comp: str, prob: float, as_of: str):
    """기존 조사 에이전트 (LLM). 테스트에서는 INVESTIGATE를 가짜 함수로 바꿔 끼운다."""
    from agents.investigator import investigate_one

    return investigate_one(machine_id, comp, prob, as_of)


INVESTIGATE = _investigate


def _alert_lock(alert_id: str) -> threading.Lock:
    with _locks_guard:
        return _alert_locks.setdefault(alert_id, threading.Lock())


def get_alert(alert_id: str) -> dict | None:
    rows = app_db._rows("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))
    return rows[0] if rows else None


def _update_alert(alert_id: str, **fields) -> None:
    fields["updated_at"] = app_db.now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with app_db._lock, app_db.connect() as conn:
        conn.execute(f"UPDATE alerts SET {sets} WHERE alert_id = ?", (*fields.values(), alert_id))


def _investigations_last_hour() -> int:
    since = (pd.Timestamp(app_db.now()) - pd.Timedelta(hours=1)).isoformat(timespec="seconds")
    return app_db._rows("SELECT COUNT(*) AS n FROM alerts WHERE investigated_at >= ?", (since,))[0]["n"]


def _comp_ko(comp: str) -> str:
    from config import COMP_LABEL_KO

    return COMP_LABEL_KO.get(comp, comp)


def handle_alert(alert_id: str) -> dict:
    """경고 1건: 조사(기존 조사 에이전트) -> 작업 종류 결정 -> 가장 빨리 비는 팀에 긴급 점검 자동 배정.

    - 같은 경고는 한 번만 처리한다 (alert_id 잠금 + 상태가 열림/대기일 때만).
    - 시간당 자동 조사 10건을 넘으면 조사만 생략하고 기본 점검 항목으로 바로 배정한다 (긴급 점검이 한도에 막히지 않게).
    - 조사 실패 -> "종료" + 관리자 알림. 교체 작업 지시·발주는 만들지 않는다 (기존처럼 관리자 승인 흐름).
    반환: {"alert_id", "status", "wo_id"?, "error"?}
    """
    lock = _alert_lock(alert_id)
    if not lock.acquire(blocking=False):
        return {"alert_id": alert_id, "status": "처리 중", "skipped": True}
    try:
        a = get_alert(alert_id)
        if a is None:
            return {"alert_id": alert_id, "status": None, "error": f"없는 경고입니다: {alert_id}"}
        if a["status"] not in ("열림", "대기"):
            return {"alert_id": alert_id, "status": a["status"], "skipped": True}
        if _investigations_last_hour() >= MAX_INVESTIGATIONS_PER_HOUR:
            message = f"{a['message']} · 시간당 조사 {MAX_INVESTIGATIONS_PER_HOUR}건 초과로 조사 생략"
            _update_alert(alert_id, message=message)
            task = "sensor_check" if a["level"] == "사람 확인" else "urgent_check"
            checklist = SENSOR_CHECKLIST if task == "sensor_check" else URGENT_CHECKLIST[a["comp"]]
            wo = dispatch_urgent({**a, "message": message}, task, list(checklist))
            return {"alert_id": alert_id, "status": "점검 배정", "wo_id": wo["wo_id"], "work_order": wo,
                    "investigated": False}
        _update_alert(alert_id, status="조사 중", investigating=1, investigated_at=app_db.now())
        try:
            finding, evidence_id = INVESTIGATE(a["machine_id"], a["comp"], float(a["prob"] or 0.0), a["as_of"])
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:300]
            _update_alert(alert_id, status="종료", investigating=0, message=f"{a['message']} · 자동 조사 실패: {error}")
            app_db.notify(title=f"#{a['machine_id']} 자동 조사 실패", body=error, to_role="admin",
                          as_of_date=a["as_of"][:10], kind="notice",
                          head=f"#{a['machine_id']} {_comp_ko(a['comp'])}", line="자동 조사 실패 · 경고 종료")
            return {"alert_id": alert_id, "status": "종료", "error": error}
        _update_alert(alert_id, investigating=0, evidence_id=evidence_id)

        from tools.plan_tools import SENSOR_LABEL

        sensor_issue = a["level"] == "사람 확인" or getattr(finding, "label", None) == SENSOR_LABEL
        task = "sensor_check" if sensor_issue else "urgent_check"
        checklist = [st.action for st in getattr(finding, "steps", [])] or list(getattr(finding, "checklist", []))
        wo = dispatch_urgent({**a, "evidence_id": evidence_id}, task, checklist)
        return {"alert_id": alert_id, "status": "점검 배정", "wo_id": wo["wo_id"], "work_order": wo}
    finally:
        lock.release()


def now_hm(cfg: dict | None = None) -> str:
    """지금 실제 시계의 "HH:MM" (초가 있으면 올려서 분 단위). 긴급 출동 시각은 감지 경로(실시간 스트림 / 입력 데이터)와
    관계없이 이 값이다 (스트림의 sim_ts는 쓰지 않는다). 화면 입력칸은 없다.
    테스트용 override: NOW_HM을 바꿔 끼우거나(같은 프로세스), 환경변수 URGENT_NOW_HM="HH:MM"(생성기 같은 별도 프로세스)."""
    if override := os.environ.get("URGENT_NOW_HM"):
        return override
    t = datetime.now()
    if t.second or t.microsecond:
        t += timedelta(minutes=1)
    return f"{t:%H:%M}"


NOW_HM = now_hm  # 테스트에서 바꿔 끼운다 (live_ops.NOW_HM = lambda cfg: "10:00")
OFF_HOURS_TAG = "근무 시간 외 출동"


def urgent_start(day: str, cfg: dict) -> dict:
    """출동 시각 = 기준 날짜 + 지금 시각. {"day", "start", "off_hours"(근무 시간 밖인데 지금 출동), "next_day"}.
    근무 시간 밖이면 운영 설정 urgent_off_hours를 따른다: "now" 그래도 지금 시각(근무 시간 외 출동),
    "next_shift" 근무 시작 전이면 오늘 근무 시작, 근무 종료 후면 다음 근무일 근무 시작.
    점심시간 중이어도 긴급은 지금 바로 시작한다."""
    now, ws, we = NOW_HM(cfg), cfg["work_start"], cfg["work_end"]
    if ws <= now < we:
        return {"day": day, "start": now, "off_hours": False, "next_day": False}
    if cfg.get("urgent_off_hours", "now") == "next_shift":
        if now < ws:
            return {"day": day, "start": ws, "off_hours": False, "next_day": False}
        nxt = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return {"day": nxt, "start": ws, "off_hours": False, "next_day": True}
    return {"day": day, "start": now, "off_hours": True, "next_day": False}


def _place_after_lunch(start: int, minutes: int, lunch: tuple[int, int]) -> tuple[int, int]:
    """밀린 작업 놓기: 점심 중에 시작하게 되면 점심 끝으로, 점심에 걸치면 점심 전후로 쪼갠다(끝 = 점심만큼 늦게)."""
    ls, le = lunch
    if ls <= start < le:
        start = le
    end = start + minutes
    if start < ls < end:
        end += le - ls
    return start, end


def reschedule_for_urgent(jobs: list[dict], start: str, minutes: int, cfg: dict) -> dict:
    """한 팀의 그날 작업에 긴급 점검을 start부터 넣고 다른 작업을 민다 (순수 함수, DB를 읽지 않음).

    jobs: [{"wo_id", "start", "end", "status", "urgent"}]. 반환 {"start", "end", "changes": [{"wo_id", "start", "end",
    "paused", "note"}], "delay": 이미 잡힌 긴급 때문에 늦춘 분}.
    - 이미 잡힌 긴급 작업은 밀지 않는다: 새 긴급과 겹치면 새 긴급을 그 뒤(+준비 시간)로.
    - ② 진행 중 작업이 긴급 시작 시각에 걸리면 그 시각에 멈추고, 남은 시간을 긴급 뒤로 이어 붙인다 (paused에 중단 구간).
    - ③ 시작 안 한 작업은 긴급(과 이어 붙인 작업) 뒤로 시작 순서대로 민다. 겹치지 않는 작업은 그대로.
    - ⑤ 밀린 작업이 점심에 걸리면 점심 전후로 쪼갠다. 긴급 자체는 점심이어도 바로 시작한다.
    - 근무 종료를 넘겨도 다음 날로 넘기지 않는다 (밀린 결과 그대로)."""
    from tools.plan_tools import _fmt, _hm

    prep = int(cfg["prep_min"])
    lunch = (_hm(cfg["lunch_start"]), _hm(cfg["lunch_end"]))
    live = sorted((j for j in jobs if j.get("status") != "DONE" and j.get("start") and j.get("end")),
                  key=lambda j: (_hm(j["start"]), j["wo_id"]))
    s0 = s = _hm(start)
    moved = True
    while moved:  # 이미 잡힌 긴급과 겹치면 그 뒤로
        moved = False
        for j in live:
            if j.get("urgent") and _hm(j["start"]) < s + minutes and _hm(j["end"]) + prep > s:
                s, moved = _hm(j["end"]) + prep, True
    e = s + minutes
    cursor, changes = e + prep, []
    for j in live:
        if j.get("urgent"):
            continue
        js, je = _hm(j["start"]), _hm(j["end"])
        if je <= s:  # 긴급 전에 끝나는 작업
            continue
        if j["status"] == "IN_PROGRESS":
            if js < s:  # ② 중단 -> 남은 시간을 긴급 뒤로
                rs, re_ = _place_after_lunch(cursor, je - s, lunch)
                changes.append({"wo_id": j["wo_id"], "start": j["start"], "end": _fmt(re_),
                                "paused": f"{_fmt(s)}~{_fmt(rs)}",
                                "note": f"{_fmt(s)}에 중단, {_fmt(rs)}에 이어서"})
                cursor = re_ + prep
            continue
        if js >= cursor:  # ③ 겹치지 않으면 그대로 (뒤 작업도 이미 그 뒤에 있다)
            cursor = je + prep
            continue
        ns, ne = _place_after_lunch(cursor, je - js, lunch)
        lunch_split = ns < lunch[0] < ne
        changes.append({"wo_id": j["wo_id"], "start": _fmt(ns), "end": _fmt(ne), "paused": None,
                        "note": f"{j['start']} → {_fmt(ns)}" + (" (점심 전후로 나눔)" if lunch_split else "")})
        cursor = ne + prep
    return {"start": _fmt(s), "end": _fmt(e), "changes": changes, "delay": s - s0}


def choose_urgent_team(jobs_by_team: dict[str, list[dict]], teams_info: list[dict], skill: str, start: str,
                       minutes: int, cfg: dict) -> tuple[str, dict, str]:
    """긴급 점검 팀 고르기: 팀마다 reschedule_for_urgent로 넣어 보고 (시작 시각, 진행 중 작업 중단 여부, 밀리는 작업 수,
    팀 순서)가 가장 나은 팀. 전문 팀(공통 작업이면 모든 팀)이 가장 빠른 팀보다 SKILL_WAIT_LIMIT_MIN 이내로 늦으면 전문 팀.
    반환 (팀, 일정 결과, 사유)."""
    from config import SKILL_WAIT_LIMIT_MIN
    from tools.plan_tools import COMMON_SKILL, _hm

    teams = [t["team"] for t in teams_info]
    plans = {t: reschedule_for_urgent(jobs_by_team.get(t, []), start, minutes, cfg) for t in teams}

    def cost(t: str) -> tuple:
        p = plans[t]
        return (_hm(p["start"]), any(c["paused"] for c in p["changes"]), len(p["changes"]), teams.index(t))

    best = min(teams, key=cost)
    if skill == COMMON_SKILL:
        return best, plans[best], "공통 작업 → 일정이 가장 덜 흔들리는 팀"
    skilled = [t["team"] for t in teams_info if t["skill"] == skill]
    if skilled:
        top = min(skilled, key=cost)
        if _hm(plans[top]["start"]) - _hm(plans[best]["start"]) <= SKILL_WAIT_LIMIT_MIN:
            return top, plans[top], f"{skill} 전문 팀"
    return best, plans[best], f"{skill} 전문 팀이 늦어 가장 빠른 팀"


def dispatch_urgent(alert: dict, task_type: str, checklist: list[str]) -> dict:
    """긴급 점검 작업 지시(UWO, ASSIGNED, urgent=1)를 지금 시각에 바로 만든다 (관리자 승인 없음).

    - 출동 시각 = 기준 날짜 + 지금 실제 시각 (urgent_start, 근무 시간 밖이면 운영 설정 urgent_off_hours).
    - 팀: choose_urgent_team (전문 팀 우선). 그 팀의 그날 다른 작업은 reschedule_for_urgent 규칙으로 민다.
    - 알림: 해당 팀 작업자 "긴급 점검: #45 베어링 — 14:20 출동", 관리자 "#45 긴급 점검을 2팀에 자동 배정했습니다",
      작업이 밀렸으면 그 팀에 "일정 변경" 안내."""
    from ops_settings import load_settings
    from tools.plan_tools import load_teams, load_work_standards

    cfg = load_settings()
    m, comp = int(alert["machine_id"]), alert["comp"]
    std = load_work_standards()
    skill = std[("sensor_check", "")]["skill"] if task_type == "sensor_check" else std[("replace", comp)]["skill"]
    minutes = int(cfg["urgent_check_min"])
    when_info = urgent_start(pd.Timestamp(alert["as_of"]).strftime("%Y-%m-%d"), cfg)
    day, tomorrow, off_hours = when_info["day"], when_info["next_day"], when_info["off_hours"]
    jobs_by_team: dict[str, list[dict]] = {}
    for w in app_db.work_orders(as_of_date=day):
        if w.get("team"):
            jobs_by_team.setdefault(w["team"], []).append(w)
    team, plan, reason = choose_urgent_team(jobs_by_team, load_teams(), skill, when_info["start"], minutes, cfg)

    created = app_db.now()
    tag = " (내일 첫 작업)" if tomorrow else f" ({OFF_HOURS_TAG})" if off_hours else ""
    title = f"#{m} {_comp_ko(comp)} {TASK_KO[task_type]}{tag}"
    prefix = f"UWO-{pd.Timestamp(day):%m%d}-{m:03d}"
    with app_db._lock, app_db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM work_orders WHERE wo_id LIKE ?", (f"{prefix}-%",)).fetchone()[0]
        wo_id = f"{prefix}-{n + 1}"
        for c in plan["changes"]:  # 밀린 작업 (진행 중이면 중단 구간도)
            conn.execute('UPDATE work_orders SET start = ?, "end" = ?, paused = COALESCE(?, paused) WHERE wo_id = ?',
                         (c["start"], c["end"], c["paused"], c["wo_id"]))
        conn.execute(
            'INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end", checklist,'
            " evidence_id, title, status, approved_by, approved_at, created_at, urgent)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ASSIGNED', ?, ?, ?, 1)",
            (wo_id, day, m, comp, task_type, team, plan["start"], plan["end"],
             json.dumps(checklist, ensure_ascii=False), alert.get("evidence_id"), title, AUTO_APPROVER, created, created))
        evidence = f" (근거 {alert['evidence_id']})" if alert.get("evidence_id") else ""
        when = f"내일 {plan['start']}" if tomorrow else plan["start"]
        off = f" · {OFF_HOURS_TAG}" if off_hours else ""
        app_db._notify(conn, title=f"{TASK_KO[task_type]}: #{m} {_comp_ko(comp)} — {when} 출동{tag}{evidence}",
                       body=f"{alert['message']}{evidence}", to_role="worker", to_team=team, link_wo_id=wo_id,
                       as_of_date=day, kind="urgent", head=f"#{m} {_comp_ko(comp)}",
                       line=f"{TASK_KO[task_type]} · {when} 출동 · {team}{off}")
        moved = f" · 작업 {len(plan['changes'])}건 밀림" if plan["changes"] else ""
        app_db._notify(conn, title=f"#{m} {TASK_KO[task_type]}을 {team}에 자동 배정했습니다",
                       body=(f"부품 {_comp_ko(comp)}, {team} {when}-{plan['end']} 출동 ({reason}), "
                             f"판정 {alert['level']}({alert['source']})" + (f" · {OFF_HOURS_TAG}" if off_hours else "")
                             + (" · 근무 종료 후라 다음 근무 시작으로 잡았습니다" if tomorrow else "")
                             + "".join(f"\n- {c['wo_id']}: {c['note']}" for c in plan["changes"])),
                       to_role="admin", link_wo_id=wo_id, as_of_date=day, kind="assign",
                       head=f"#{m} {_comp_ko(comp)}", line=f"{TASK_KO[task_type]} → {team} {when}{off}{moved}")
        if plan["changes"]:
            app_db._notify(conn, title=f"일정 변경: {team} 긴급 점검으로 작업 {len(plan['changes'])}건 조정",
                           body="\n".join(f"{c['wo_id']}: {c['note']}" for c in plan["changes"]),
                           to_role="worker", to_team=team, as_of_date=day, kind="notice",
                           head=f"{team} 일정 변경", line=f"긴급 점검 {when} · 작업 {len(plan['changes'])}건 뒤로")
    _update_alert(alert["alert_id"], status="점검 배정", wo_id=wo_id)
    return {**app_db.get_work_order(wo_id), "tomorrow": tomorrow, "off_hours": off_hours, "changes": plan["changes"]}


def mock_investigate(machine_id: int, comp: str, prob: float, as_of: str):
    """목업 모드용 조사 (LLM 없음): 고정 점검 단계로 바로 결과를 낸다. 근거 파일은 만들지 않는다."""
    from types import SimpleNamespace

    steps = [SimpleNamespace(action=a) for a in ("설비 정지·잠금 확인", f"{_comp_ko(comp)} 상태 점검", "재가동 후 센서 값 확인")]
    return SimpleNamespace(label="예측 고장", steps=steps), None


# ---------------------------------------------------------------- 4단계 화면용 조회
def alert_progress(a: dict) -> str:
    """진행 상태 한 줄: 조사 중 / 2팀 14:20 | 출동 확인 대기 / 2팀 14:20 | 출동 / 점검 완료 ..."""
    status = a.get("status")
    where = f"{a.get('team')} {a.get('start')}" if a.get("team") else ""
    if status == "점검 배정":
        return f"{where} | 출동 확인 대기" if where else "점검 배정"
    if status == "출동":
        return f"{where} | 출동" if where else "출동"
    if status == "대기":
        return "조사 대기 (시간당 한도)"
    if status == "열림":
        return "기록만 (배정 없음)" if a.get("level") == "주의" else "접수"
    return status or "-"


def scenario_preview(machine_id: int, kind: str, as_of: str, hours: int = 18) -> pd.DataFrame:
    """미리보기 차트용 (저장하지 않음). 열: datetime, 원본, 입력 (짝 센서 값)"""
    _, sensor, _ = SCENARIOS[kind]
    telemetry, _ = scenario_frames(machine_id, kind, as_of, hours)
    times = [pd.Timestamp(t) for t in telemetry["datetime"]]
    return pd.DataFrame({"datetime": times, "원본": _raw_hours(machine_id, times)[sensor].to_numpy(),
                         "입력": telemetry[sensor].to_numpy()})


def urgent_orders(day: str) -> list[dict]:
    """그날 긴급 작업 지시 (urgent=1, 간트·경고 표용)."""
    return [w for w in app_db.work_orders(as_of_date=_ts(day)[:10]) if w.get("urgent")]


def live_version() -> str:
    """입력 데이터가 바뀌었는지 알아보는 값 (화면 캐시 키용)."""
    try:
        r = app_db._rows("SELECT COUNT(*) AS n, MAX(created_at) AS t, SUM(active) AS a FROM live_batches")[0]
        return f"{r['n']}|{r['t']}|{r['a']}"
    except Exception:
        return "-"


def on_work_started(wo_id: str) -> None:
    """작업자가 긴급 작업에 [출동 확인](작업 시작)을 누르면 경고 상태를 "출동"으로."""
    for a in app_db._rows("SELECT alert_id FROM alerts WHERE wo_id = ? AND status = '점검 배정'", (wo_id,)):
        _update_alert(a["alert_id"], status="출동")


def on_work_done(wo_id: str) -> None:
    """긴급 작업 완료 보고 -> 경고 상태 "점검 완료" + 실시간 스트림에 정비 완료 신호 (값이 평소로 내려오게)."""
    from stream.control import mark_repaired

    for a in app_db._rows("SELECT alert_id, machine_id FROM alerts WHERE wo_id = ?", (wo_id,)):
        _update_alert(a["alert_id"], status="점검 완료")
        mark_repaired(int(a["machine_id"]), by="work_done")


def open_alerts(as_of: str) -> list[dict]:
    """기준 날짜의 닫히지 않은 경고 (최신순). 배정된 작업 지시(팀·시각·상태)를 함께."""
    day = _ts(as_of)[:10]
    return app_db._rows(
        "SELECT a.*, w.team, w.start, w.\"end\", w.status AS wo_status FROM alerts a"
        f" LEFT JOIN work_orders w ON w.wo_id = a.wo_id WHERE substr(a.as_of, 1, 10) = ?"
        f" AND a.status NOT IN ({', '.join('?' * len(CLOSED))}) ORDER BY a.updated_at DESC, a.alert_id DESC",
        (day, *CLOSED))


def alert_history(limit: int = 50) -> list[dict]:
    return app_db._rows(
        "SELECT a.*, w.team, w.start, w.\"end\", w.status AS wo_status FROM alerts a"
        " LEFT JOIN work_orders w ON w.wo_id = a.wo_id ORDER BY a.created_at DESC, a.alert_id DESC LIMIT ?",
        (int(limit),))


def clear_live_data() -> dict:
    """입력 데이터를 모두 지운다 (실시간 스트림 원시값과 STREAM 요약 배치 포함). 입력 데이터로 생긴 닫히지 않은 경고는
    "종료"로 닫는다 (이미 배정된 긴급 작업 지시는 남긴다). 반환 {"batches", "telemetry", "errors", "stream", "alerts_closed"}"""
    with app_db._lock, app_db.connect() as conn:
        out = {"telemetry": conn.execute("DELETE FROM live_telemetry").rowcount,
               "errors": conn.execute("DELETE FROM live_errors").rowcount,
               "batches": conn.execute("DELETE FROM live_batches").rowcount,  # STREAM-YYYYMMDD 요약 배치 포함
               "stream": conn.execute("DELETE FROM live_stream").rowcount,
               "alerts_closed": conn.execute(
                   "UPDATE alerts SET status = '종료', investigating = 0, message = message || ' · 입력 데이터 삭제로 종료',"
                   f" updated_at = ? WHERE batch_id IS NOT NULL AND status NOT IN ({', '.join('?' * len(CLOSED))})",
                   (app_db.now(), *CLOSED)).rowcount}
    return out


# 목업 모드 화면(S.live())도 같은 이름으로 쓰도록 스트림 제어를 함께 내보낸다 (services와 같은 함수)
from stream.control import (  # noqa: E402, F401
    stream_card, stream_inject, stream_reset, stream_start, stream_status, stream_stop, stream_window,
)
