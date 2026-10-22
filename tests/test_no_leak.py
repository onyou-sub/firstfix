"""미래 데이터 누출 방지 테스트: 도구와 내부 조회 함수는 기준 시각(as_of) 이후 데이터를 절대 돌려주지 않는다.

실행: uv run pytest tests/test_no_leak.py -v
"""

import re
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from tools.context import Ctx
from tools.data_tools import (
    COMPS,
    DATA_TOOLS,
    DB_PATH,
    MODELS,
    SENSORS,
    _error_rows,
    _maint_rows,
    _sensor_rows,
    _similar_failure_rows,
    find_similar_failures,
    get_error_logs,
    get_maintenance_history,
    get_sensor_trend,
    search_manual,
    sensor_series,
)

AS_OFS = ["2015-10-01 06:00:00", "2015-11-12 06:00:00", "2015-12-22 06:00:00"]
MACHINES = [13, 24, 84]  # 데모·백테스트에 나온 설비
BEFORE_ALL_DATA = "2014-01-01 00:00:00"  # telemetry·errors·maint·failures 어느 것보다 이른 시각


def runtime(as_of: str) -> SimpleNamespace:
    """ToolRuntime[Ctx] 대신 쓰는 최소 객체 (도구는 runtime.context.as_of만 읽는다)."""
    return SimpleNamespace(context=Ctx(as_of=as_of))


def machine_model(machine_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT model FROM machines WHERE machine_id = ?", (machine_id,)).fetchone()[0]


def assert_not_after(df: pd.DataFrame, as_of: str) -> None:
    if len(df):
        assert df["datetime"].max() <= pd.Timestamp(as_of), f"as_of({as_of}) 이후 데이터: {df['datetime'].max()}"


# ---------------------------------------------------------------- 1. 내부 조회 함수: 최대 datetime <= as_of
@pytest.mark.parametrize("as_of", AS_OFS)
@pytest.mark.parametrize("machine_id", MACHINES)
@pytest.mark.parametrize("sensor", SENSORS)
def test_sensor_rows_not_after_as_of(as_of, machine_id, sensor):
    df = _sensor_rows(machine_id, sensor, 168, as_of)
    assert len(df) > 0
    assert_not_after(df, as_of)
    assert df["datetime"].max() == pd.Timestamp(as_of)  # as_of 시각 자체는 포함 (이하 조건)


@pytest.mark.parametrize("as_of", AS_OFS)
@pytest.mark.parametrize("machine_id", MACHINES)
def test_error_rows_not_after_as_of(as_of, machine_id):
    assert_not_after(_error_rows(machine_id, 30, as_of), as_of)


@pytest.mark.parametrize("as_of", AS_OFS)
@pytest.mark.parametrize("machine_id", MACHINES)
def test_maint_rows_not_after_as_of(as_of, machine_id):
    df = _maint_rows(machine_id, as_of)
    assert len(df) > 0
    assert_not_after(df, as_of)


@pytest.mark.parametrize("as_of", AS_OFS)
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("comp", COMPS)
def test_similar_failure_rows_not_after_as_of(as_of, model, comp):
    assert_not_after(_similar_failure_rows(model, comp, as_of, limit=3), as_of)


@pytest.mark.parametrize("as_of", AS_OFS)
def test_sensor_series_not_after_as_of(as_of):
    assert_not_after(sensor_series(13, "volt", 72, as_of), as_of)


# ---------------------------------------------------------------- 2. as_of 이후 구간만 해당하는 요청은 0행
def test_requests_before_any_data_return_zero_rows():
    assert len(_sensor_rows(13, "volt", 168, BEFORE_ALL_DATA)) == 0
    assert len(_error_rows(13, 30, BEFORE_ALL_DATA)) == 0
    assert len(_maint_rows(13, BEFORE_ALL_DATA)) == 0
    for model in MODELS:
        for comp in COMPS:
            assert len(_similar_failure_rows(model, comp, BEFORE_ALL_DATA)) == 0


def test_event_one_hour_later_is_invisible():
    """설비 13은 2015-12-22 06:00에 motor 고장·교체. 1시간 전 기준으로는 보이지 않아야 한다."""
    event = pd.Timestamp("2015-12-22 06:00:00")
    before = "2015-12-22 05:00:00"
    assert event not in set(_maint_rows(13, before)["datetime"])
    assert event in set(_maint_rows(13, event.strftime("%Y-%m-%d %H:%M:%S"))["datetime"])
    assert _sensor_rows(13, "volt", 1, before)["datetime"].max() == pd.Timestamp(before)

    text = get_maintenance_history.func(13, runtime(before))
    assert "2015-12-22 06:00" not in text


# ---------------------------------------------------------------- 3. find_similar_failures가 미래 고장을 사례로 내놓지 않음
@pytest.mark.parametrize("as_of", AS_OFS)
@pytest.mark.parametrize("comp", COMPS)
def test_find_similar_failures_has_no_future_cases(as_of, comp):
    model = machine_model(13)
    text = find_similar_failures.func(model, comp, runtime(as_of))
    times = [pd.Timestamp(t) for t in re.findall(r"\| (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \|", text)]
    assert times, text
    assert max(times) <= pd.Timestamp(as_of), text


def test_find_similar_failures_hides_failure_right_after_as_of():
    """2015-12-22 06:00 설비 13 motor 고장은 05:00 기준 사례 목록에 없어야 한다."""
    model = machine_model(13)
    before = _similar_failure_rows(model, "motor", "2015-12-22 05:00:00", limit=100)
    at = _similar_failure_rows(model, "motor", "2015-12-22 06:00:00", limit=100)
    key = (pd.Timestamp("2015-12-22 06:00:00"), 13)
    assert key not in set(zip(before["datetime"], before["machine_id"]))
    assert key in set(zip(at["datetime"], at["machine_id"]))


# ---------------------------------------------------------------- 4. 잘못된 입력은 "입력 오류" 문자열
RT = runtime(AS_OFS[0])
BAD_CALLS = [
    ("machine_id=0", lambda: get_sensor_trend.func(0, "volt", RT, 72)),
    ("machine_id=101", lambda: get_error_logs.func(101, RT, 7)),
    ("machine_id=문자", lambda: get_maintenance_history.func("abc", RT)),
    ("sensor=temp", lambda: get_sensor_trend.func(13, "temp", RT, 72)),
    ("hours=0", lambda: get_sensor_trend.func(13, "volt", RT, 0)),
    ("hours=169", lambda: get_sensor_trend.func(13, "volt", RT, 169)),
    ("days=0", lambda: get_error_logs.func(13, RT, 0)),
    ("days=31", lambda: get_error_logs.func(13, RT, 31)),
    ("model=model9", lambda: find_similar_failures.func("model9", "motor", RT)),
    ("comp=gear", lambda: find_similar_failures.func("model3", "gear", RT)),
    ("query=빈 문자열", lambda: search_manual.func("  ")),
]


@pytest.mark.parametrize("name,call", BAD_CALLS, ids=[n for n, _ in BAD_CALLS])
def test_invalid_input_returns_error_string(name, call):
    result = call()
    assert isinstance(result, str) and result.startswith("입력 오류"), result


# ---------------------------------------------------------------- 5. 도구 스키마에 as_of가 없음 (LLM이 기준 시각을 못 바꿈)
@pytest.mark.parametrize("t", DATA_TOOLS, ids=[t.name for t in DATA_TOOLS])
def test_tool_schema_has_no_as_of(t):
    llm_args = set(t.tool_call_schema.model_json_schema().get("properties", {}))
    for forbidden in ("as_of", "runtime"):
        assert forbidden not in t.args, f"{t.name}.args에 {forbidden}"
        assert forbidden not in llm_args, f"{t.name} LLM 스키마에 {forbidden}"
