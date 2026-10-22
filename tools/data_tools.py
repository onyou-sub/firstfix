"""설비 데이터 조회 도구 (LangChain @tool 5개) + 내부 조회 함수.

- 내부 조회 함수(_sensor_rows 등)는 반드시 as_of를 받고, 모든 SQL에 datetime <= as_of를 붙인다.
- 도구는 as_of를 ToolRuntime[Ctx].context에서 꺼낸다. LLM 입력 인자에는 as_of가 없어서 기준 시각을 바꿀 수 없다.
- 도구는 원본 행 대신 요약 문자열을 돌려준다 (표는 최대 10행). 잘못된 입력은 예외 대신 "입력 오류: ..." 문자열.
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
from langchain.tools import ToolRuntime, tool

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "data" / "virtual"))

from config import COMP_LABEL_KO, ERROR_LABEL_KO  # noqa: E402
from tools.context import Ctx  # noqa: E402
from store import app_db, live_data  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "pdm.db"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

SENSORS = ["volt", "rotate", "pressure", "vibration"]
SENSOR_MEAN = {"volt": 170.8, "rotate": 446.6, "pressure": 100.9, "vibration": 40.4}  # 1년 평균 (고정 기준값)
SENSOR_LABEL = {"volt": "전압(V)", "rotate": "회전수(rpm)", "pressure": "압력(kPa)", "vibration": "진동(mm/s)"}
COMPS = ["motor", "drive", "pump", "bearing"]
COMP_SENSOR = {"motor": "volt", "drive": "rotate", "pump": "pressure", "bearing": "vibration"}  # 고장 전 패턴 센서
MODELS = ["model1", "model2", "model3", "model4"]
MAX_TABLE_ROWS = 10
TREND_BAND_PCT = 3.0  # 전반부 대비 후반부 평균 변화가 1년 평균의 ±3% 이내면 "유지"
RECENT_REPLACE_DAYS = 7


# ---------------------------------------------------------------- 공통
def _norm_as_of(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime(DATETIME_FORMAT)


def _since(as_of: str, hours: float) -> str:
    return (pd.Timestamp(as_of) - pd.Timedelta(hours=hours)).strftime(DATETIME_FORMAT)


def _query(sql: str, params: dict) -> pd.DataFrame:
    assert "as_of" in params and ":as_of" in sql, "모든 조회는 as_of 조건을 붙여야 합니다"
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], format=DATETIME_FORMAT)
    return df


def _check_sensor(sensor: str) -> None:
    if sensor not in SENSORS:  # 컬럼 이름이 SQL에 들어가므로 화이트리스트로만 허용
        raise ValueError(f"sensor는 {SENSORS} 중 하나여야 합니다: {sensor!r}")


def _md(df: pd.DataFrame) -> str:
    df = df.head(MAX_TABLE_ROWS)
    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(map(str, row)) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def _fmt_time(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def _pct(value: float, base: float) -> str:
    return f"{(value / base - 1) * 100:+.1f}%"


def _range_error(name: str, value, lo: int, hi: int) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return f"입력 오류: {name}는 {lo}~{hi} 사이 정수여야 합니다 (받은 값: {value!r})"
    return None


def _choice_error(name: str, value, choices: list[str]) -> str | None:
    if value not in choices:
        return f"입력 오류: {name}는 {', '.join(choices)} 중 하나여야 합니다 (받은 값: {value!r})"
    return None


def _first_error(*errors: str | None) -> str | None:
    return next((e for e in errors if e), None)


# ---------------------------------------------------------------- 내부 조회 함수 (테스트용, 모두 datetime <= as_of)
def _with_batches(out: pd.DataFrame, merged: pd.DataFrame) -> pd.DataFrame:
    """섞인 입력 배치 번호를 df.attrs["input_batches"]에 남긴다 (열 형식은 그대로)."""
    out.attrs["input_batches"] = live_data.input_batches(merged)
    return out


def _sensor_rows(machine_id: int, sensor: str, hours: int, as_of: str) -> pd.DataFrame:
    """(as_of - hours, as_of] 구간 센서 값 (원본 + 켜진 입력 데이터). 열: datetime, value"""
    _check_sensor(sensor)
    as_of = _norm_as_of(as_of)
    df = live_data.telemetry_frame([machine_id], _since(as_of, hours), as_of)
    return _with_batches(df[["datetime", sensor]].rename(columns={sensor: "value"}), df)


def _error_rows(machine_id: int, days: int, as_of: str) -> pd.DataFrame:
    """(as_of - days, as_of] 구간 에러 (원본 + 켜진 입력 데이터). 열: datetime, error_id"""
    as_of = _norm_as_of(as_of)
    df = live_data.errors_frame([machine_id], _since(as_of, days * 24), as_of)
    return _with_batches(df[["datetime", "error_id"]].copy(), df)


def _maint_rows(machine_id: int, as_of: str) -> pd.DataFrame:
    """as_of 이하 교체 기록 전체 = 원본(pdm.db maint) + 작업자 보고(app.db maint_log).
    열: datetime, comp, by_failure, source("원본" / "작업자 보고")"""
    as_of = _norm_as_of(as_of)
    base = _query(
        "SELECT datetime, comp, by_failure FROM maint"
        " WHERE machine_id = :m AND datetime <= :as_of ORDER BY datetime",
        {"m": machine_id, "as_of": as_of},
    ).assign(source="원본")
    worker = app_db.maint_rows(machine_id, as_of)
    if worker.empty:
        return base
    worker = worker.assign(by_failure=0)[["datetime", "comp", "by_failure", "source"]]
    return pd.concat([base, worker], ignore_index=True).sort_values("datetime", kind="stable").reset_index(drop=True)


def _similar_failure_rows(model: str, comp: str, as_of: str, limit: int = 3) -> pd.DataFrame:
    """as_of 이하 같은 model·comp 고장 중 최근 limit건과 고장 전 24시간 해당 센서 평균.

    열: datetime, machine_id, sensor_mean_24h. 센서 평균 구간은 [고장-24h, 고장)이며 역시 as_of 이하만 본다.
    """
    sensor = COMP_SENSOR[comp]
    as_of = _norm_as_of(as_of)
    return _query(
        f"""
        SELECT f.datetime, f.machine_id,
               (SELECT AVG(t.{sensor}) FROM telemetry t
                 WHERE t.machine_id = f.machine_id
                   AND t.datetime >= datetime(f.datetime, '-24 hours')
                   AND t.datetime < f.datetime
                   AND t.datetime <= :as_of) AS sensor_mean_24h
          FROM failures f JOIN machines m ON f.machine_id = m.machine_id
         WHERE m.model = :model AND f.comp = :comp AND f.datetime <= :as_of
         ORDER BY f.datetime DESC
         LIMIT :limit
        """,
        {"model": model, "comp": comp, "as_of": as_of, "limit": limit},
    )


def _count_failures(model: str, comp: str, as_of: str) -> int:
    df = _query(
        "SELECT COUNT(*) AS n FROM failures f JOIN machines m ON f.machine_id = m.machine_id"
        " WHERE m.model = :model AND f.comp = :comp AND f.datetime <= :as_of",
        {"model": model, "comp": comp, "as_of": _norm_as_of(as_of)},
    )
    return int(df["n"].iloc[0])


# ---------------------------------------------------------------- 화면용 (도구 아님)
def sensor_series(machine_id: int, sensor: str, hours: int, as_of: str) -> pd.DataFrame:
    """차트용 센서 시계열. 열: datetime, value, baseline(1년 평균), source("원본" / "입력")"""
    _check_sensor(sensor)
    as_of = _norm_as_of(as_of)
    df = live_data.telemetry_frame([machine_id], _since(as_of, hours), as_of)
    return pd.DataFrame({"datetime": df["datetime"], "value": df[sensor], "baseline": SENSOR_MEAN[sensor],
                         "source": df["source"]})


# ---------------------------------------------------------------- 도구 본문 (runtime 없이 as_of로 호출 가능)
def sensor_trend_text(machine_id: int, sensor: str, hours: int, as_of: str) -> str:
    if err := _first_error(
        _range_error("machine_id", machine_id, 1, 100),
        _choice_error("sensor", sensor, SENSORS),
        _range_error("hours", hours, 1, 168),
    ):
        return err
    as_of = _norm_as_of(as_of)
    df = _sensor_rows(machine_id, sensor, hours, as_of)
    head = f"[설비 {machine_id} {SENSOR_LABEL[sensor]} 최근 {hours}시간, 기준 {as_of}]"
    if df.empty:
        return f"{head}\n해당 구간 센서 기록이 없습니다."

    base = SENSOR_MEAN[sensor]
    last = df.iloc[-1]
    recent24 = df.loc[df["datetime"] > pd.Timestamp(as_of) - pd.Timedelta(hours=24), "value"]
    mean24 = recent24.mean() if len(recent24) else float("nan")

    half = len(df) // 2
    if half >= 1:
        change = df["value"].iloc[half:].mean() - df["value"].iloc[:half].mean()
        change_pct = change / base * 100
        trend = "상승" if change_pct > TREND_BAND_PCT else "하락" if change_pct < -TREND_BAND_PCT else "유지"
        trend_text = f"{trend} (전반부 대비 후반부 평균 {change_pct:+.1f}%p, 1년 평균 기준)"
    else:
        trend_text = "판단 불가 (기록 1건)"

    # 구간 요약 표: 24시간 초과면 일 단위, 아니면 3시간 단위 평균 (최근 10구간)
    freq = "24h" if hours > 24 else "3h"
    buckets = (
        df.set_index("datetime")["value"]
        .resample(freq, origin=pd.Timestamp(as_of), closed="right", label="right")
        .agg(["mean", "count"])
        .dropna()
        .tail(MAX_TABLE_ROWS)
        .reset_index()
    )
    table = pd.DataFrame({
        "구간 끝": buckets["datetime"].map(_fmt_time),
        "평균": buckets["mean"].map("{:.1f}".format),
        "1년 평균 대비": buckets["mean"].map(lambda v: _pct(v, base)),
        "건수": buckets["count"].astype(int),
    })

    batches = df.attrs.get("input_batches") or []
    return "\n".join([
        head,
        *([f"※ 입력 데이터 포함 (배치 {', '.join(batches)})"] if batches else []),
        f"- 최근값: {last['value']:.1f} ({_fmt_time(last['datetime'])})",
        f"- 24시간 평균: {mean24:.1f} (1년 평균 {base} 대비 {_pct(mean24, base)})",
        f"- 추세: {trend_text}",
        f"\n{freq.replace('h', '시간')} 단위 평균:",
        _md(table),
    ])


def error_logs_text(machine_id: int, days: int, as_of: str) -> str:
    if err := _first_error(_range_error("machine_id", machine_id, 1, 100), _range_error("days", days, 1, 30)):
        return err
    as_of = _norm_as_of(as_of)
    df = _error_rows(machine_id, days, as_of)
    head = f"[설비 {machine_id} 에러 최근 {days}일, 기준 {as_of}]"
    if df.empty:
        return f"{head}\n에러 기록이 없습니다."
    summary = (
        df.groupby("error_id")["datetime"].agg(["count", "max"])
        .sort_values(["count", "max"], ascending=False).reset_index()
    )
    last24 = df[df["datetime"] > pd.Timestamp(as_of) - pd.Timedelta(hours=24)]
    table = pd.DataFrame({
        "에러": summary["error_id"].map(lambda e: f"{e}({ERROR_LABEL_KO.get(e, e)})"),
        "횟수": summary["count"],
        "최근 24시간": summary["error_id"].map(last24["error_id"].value_counts()).fillna(0).astype(int),
        "마지막 발생": summary["max"].map(_fmt_time),
    })
    return f"{head}\n총 {len(df)}건\n{_md(table)}"


def maintenance_history_text(machine_id: int, as_of: str) -> str:
    if err := _range_error("machine_id", machine_id, 1, 100):
        return err
    as_of = _norm_as_of(as_of)
    df = _maint_rows(machine_id, as_of)
    head = f"[설비 {machine_id} 부품 교체 이력, 기준 {as_of}]"
    as_of_ts = pd.Timestamp(as_of)
    rows, recent, timing = [], [], []
    for comp in COMPS:
        c = df[df["comp"] == comp]
        if c.empty:
            rows.append({"부품": f"{comp}({COMP_LABEL_KO[comp]})", "마지막 교체": "기록 없음", "경과일": "-",
                         "교체 사유": "-", "누적 교체": 0})
            continue
        last = c.iloc[-1]
        elapsed = (as_of_ts - last["datetime"]).total_seconds() / 86400
        if elapsed <= RECENT_REPLACE_DAYS:
            recent.append(f"{comp} ({elapsed:.1f}일 전)")
            timing.append(_replacement_timing(comp, last["datetime"], elapsed * 24))
        rows.append({
            "부품": f"{comp}({COMP_LABEL_KO[comp]})",
            "마지막 교체": _fmt_time(last["datetime"]),
            "경과일": f"{elapsed:.1f}",
            "교체 사유": (f"({last['source']})" if last.get("source", "원본") != "원본"
                        else "고장 교체" if last["by_failure"] else "정기 교체"),
            "누적 교체": len(c),
        })
    note = (f"\n주의: 최근 {RECENT_REPLACE_DAYS}일 안에 교체한 부품 - {', '.join(recent)}."
            " 같은 부품 이상이 계속되면 재교체 전에 센서·설치 상태를 먼저 점검한다." if recent else "")
    note += "".join(f"\n{t}" for t in timing)
    worker = df[df["source"] != "원본"] if "source" in df else df.iloc[0:0]
    if not worker.empty:  # 작업자 완료 보고로 들어온 교체 (원본 기록보다 이르면 표의 '마지막 교체'에 안 보일 수 있음)
        note += "\n작업자 보고 교체 (최근 5건): " + ", ".join(
            f"{_fmt_time(r.datetime)} {r.comp}({COMP_LABEL_KO[r.comp]}) ({r.source})" for r in worker.tail(5).itertuples())
    return f"{head}\n{_md(pd.DataFrame(rows))}{note}"


def _replacement_timing(comp: str, replaced: pd.Timestamp, hours_since: float) -> str:
    """교체 시각 기준으로 다른 도구의 기록이 교체 전/후 어느 쪽인지 코드로 알려준다 (LLM 해석 오류 방지)."""
    when = _fmt_time(replaced)
    if hours_since < 1:
        return (f"교체 시점 안내: {comp} 교체({when})가 기준 시각과 같다. 교체 후 데이터는 아직 없다."
                " 조회되는 센서 평균·에러는 모두 교체 전 기록이므로 '교체 후에도/교체 직후에도'라고 쓰지 않는다.")
    if hours_since < 24:
        return (f"교체 시점 안내: {comp} 교체({when}) 후 {hours_since:.0f}시간 경과."
                f" 24시간 평균에는 교체 전 값이 섞여 있다. {when} 이후 기록만 교체 후 상태다.")
    return (f"교체 시점 안내: {comp} 교체({when}) 후 {hours_since / 24:.1f}일 경과."
            " 최근 24시간 센서 평균·에러는 교체 후 상태다.")


def similar_failures_text(model: str, comp: str, as_of: str) -> str:
    if err := _first_error(_choice_error("model", model, MODELS), _choice_error("comp", comp, COMPS)):
        return err
    as_of = _norm_as_of(as_of)
    sensor, base = COMP_SENSOR[comp], SENSOR_MEAN[COMP_SENSOR[comp]]
    df = _similar_failure_rows(model, comp, as_of, limit=3)
    head = f"[{model} · {comp}({COMP_LABEL_KO[comp]}) 과거 고장 사례, 기준 {as_of}까지]"
    if df.empty:
        return f"{head}\n기준 시각 이전 같은 사례가 없습니다."
    table = pd.DataFrame({
        "고장 시각": df["datetime"].map(_fmt_time),
        "설비": df["machine_id"],
        f"고장 전 24시간 {SENSOR_LABEL[sensor]} 평균": df["sensor_mean_24h"].map(
            lambda v: "-" if pd.isna(v) else f"{v:.1f}"),
        "1년 평균 대비": df["sensor_mean_24h"].map(lambda v: "-" if pd.isna(v) else _pct(v, base)),
    })
    total = _count_failures(model, comp, as_of)
    return f"{head}\n전체 {total}건 중 최근 {len(df)}건\n{_md(table)}"


def search_manual_text(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        return "입력 오류: query는 비어 있지 않은 문자열이어야 합니다"
    from build_index import search_manual_raw  # Chroma·OpenAI는 실제 검색할 때만 불러온다
    return search_manual_raw(query.strip(), k=3)


# ---------------------------------------------------------------- LangChain 도구 (as_of는 runtime.context에서)
@tool
def get_sensor_trend(machine_id: int, sensor: str, runtime: ToolRuntime[Ctx], hours: int = 72) -> str:
    """설비 한 대의 센서 값 추이를 요약한다: 최근값, 24시간 평균, 1년 평균 대비 %, 추세(상승/하락/유지), 구간별 평균 표.

    언제 쓰나: 고위험 설비의 이상 원인을 확인할 때. 부품별 관련 센서는
    motor=volt(전압 상승), drive=rotate(회전수 하락), pump=pressure(압력 상승), bearing=vibration(진동 상승).
    언제 쓰지 않나: 에러 횟수(get_error_logs), 교체 이력(get_maintenance_history)을 볼 때.

    Args:
        machine_id: 설비 번호 1~100
        sensor: volt, rotate, pressure, vibration 중 하나
        hours: 기준 시각에서 거슬러 볼 시간 1~168 (기본 72)
    """
    return sensor_trend_text(machine_id, sensor, hours, runtime.context.as_of)


@tool
def get_error_logs(machine_id: int, runtime: ToolRuntime[Ctx], days: int = 7) -> str:
    """설비 한 대의 최근 에러를 종류별 횟수, 최근 24시간 횟수, 마지막 발생 시각으로 요약한다.

    언제 쓰나: 센서 이상이 실제 경고(overvoltage, low_rpm, torque, pressure, vibration)로 이어졌는지 확인할 때.
    언제 쓰지 않나: 센서 값 자체의 추세(get_sensor_trend)나 부품 교체 이력(get_maintenance_history)을 볼 때.

    Args:
        machine_id: 설비 번호 1~100
        days: 기준 시각에서 거슬러 볼 일수 1~30 (기본 7)
    """
    return error_logs_text(machine_id, days, runtime.context.as_of)


@tool
def get_maintenance_history(machine_id: int, runtime: ToolRuntime[Ctx]) -> str:
    """설비 한 대의 부품별(motor, drive, pump, bearing) 마지막 교체일, 경과일, 고장 교체 여부를 요약한다.

    언제 쓰나: 교체를 권하기 전에 최근 교체 이력을 확인할 때. 최근 7일 안에 같은 부품을 교체했다면
    재교체보다 센서·설치 점검이 우선이므로 반드시 확인한다.
    언제 쓰지 않나: 센서 추세나 에러 횟수를 볼 때.

    Args:
        machine_id: 설비 번호 1~100
    """
    return maintenance_history_text(machine_id, runtime.context.as_of)


@tool
def find_similar_failures(model: str, comp: str, runtime: ToolRuntime[Ctx]) -> str:
    """기준 시각 이전, 같은 설비 모델·같은 부품의 과거 고장 사례 최대 3건과 고장 전 24시간 관련 센서 평균을 보여준다.

    언제 쓰나: 현재 설비의 센서 수준이 과거 고장 직전 수준과 비슷한지 비교해 근거를 보강할 때.
    언제 쓰지 않나: 특정 설비 한 대의 현재 상태를 볼 때 (get_sensor_trend 사용).

    Args:
        model: 설비 모델 model1, model2, model3, model4 중 하나
        comp: 부품 motor, drive, pump, bearing 중 하나
    """
    return similar_failures_text(model, comp, runtime.context.as_of)


@tool
def search_manual(query: str) -> str:
    """정비 매뉴얼(부품별 점검 절차, 판단 기준 수치, 교체 기준, 센서 점검, 안전 절차)을 검색해 상위 3개 절을 출처와 함께 돌려준다.

    언제 쓰나: 점검 체크리스트·판단 기준·교체 기준·정지 절차처럼 매뉴얼 근거가 필요할 때.
    예: "베어링 진동 상승 점검", "부품 교체 후에도 이상이 계속됨", "점검 전 설비 정지 절차".
    언제 쓰지 않나: 설비 데이터(센서, 에러, 교체 이력)를 조회할 때.

    Args:
        query: 검색할 내용 (한국어 문장)
    """
    return search_manual_text(query)


DATA_TOOLS = [get_sensor_trend, get_error_logs, get_maintenance_history, find_similar_failures, search_manual]
