"""작업자 대시보드 집계 (store/worker_stats.py) + 긴급 설비 실시간 자동 포함 (stream.control.stream_add_machine).
LLM 없음, 임시 app.db에 가짜 work_orders / work_reports / live_stream을 넣는다.

실행: uv run pytest tests/test_worker_stats.py -v
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from store import app_db
from store import worker_stats as ws
from ui.style import GRAY, NAVY, ORANGE, RED, RED_DARK

DAY = "2015-10-15"  # 목요일
MONTH = ("2015-10-01", "2015-10-31")


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")


def add_order(wo_id, as_of_date, machine_id=10, team="2팀", status="ASSIGNED", start="09:00", end="10:00",
              comp="motor", task_type="replace", urgent=0):
    with app_db.connect() as conn:
        conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                     " title, status, urgent, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (wo_id, as_of_date, machine_id, comp, task_type, team, start, end, f"#{machine_id}", status,
                      urgent, app_db.now()))


def add_report(wo_id, as_of_date, machine_id=10, team="2팀", cause="예측과 같음", action="부품 교체",
               actual_comp="motor", predicted_comp="motor", reported_by="team2"):
    with app_db.connect() as conn:
        conn.execute("INSERT INTO work_reports (wo_id, machine_id, as_of_date, predicted_comp, actual_cause_type,"
                     " actual_comp, action, duration_min, team, reported_by, reported_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, 60, ?, ?, ?)",
                     (wo_id, machine_id, as_of_date, predicted_comp, cause, actual_comp, action, team, reported_by,
                      app_db.now()))


def add_stream(machine_id, seconds_ago=1.0, **values):
    wall = (datetime.now() - timedelta(seconds=seconds_ago)).isoformat(timespec="milliseconds")
    v = {"volt": 170.0, "rotate": 450.0, "pressure": 100.0, "vibration": 40.0, **values}
    with app_db.connect() as conn:
        conn.execute("INSERT INTO live_stream (sim_ts, wall_ts, machine_id, volt, rotate, pressure, vibration)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (f"{DAY} 06:00:01", wall, machine_id, v["volt"], v["rotate"],
                                                     v["pressure"], v["vibration"]))


def generator_running(machines=()):
    app_db.set_state("stream_running", True)
    app_db.set_state("stream_heartbeat", datetime.now().isoformat(timespec="milliseconds"))
    app_db.set_state("stream_machines", list(machines))


# ---------------------------------------------------------------- ① 오늘 시간표
def test_today_timeline_only_day_and_team_sorted():
    add_order("W2", DAY, start="13:00", end="14:00")
    add_order("W1", DAY, start="09:30", end="11:00", urgent=1)
    add_order("W3", DAY, team="1팀")
    add_order("W4", "2015-10-14")
    df = ws.today_timeline(DAY, "2팀")
    assert list(df["wo_id"]) == ["W1", "W2"] and list(df.columns) == ws.TIMELINE_COLS
    assert str(df.loc[0, "start_dt"]) == "2015-10-15 09:30:00" and str(df.loc[0, "end_dt"]) == "2015-10-15 11:00:00"
    assert bool(df.loc[0, "urgent"]) and not bool(df.loc[1, "urgent"])
    assert ws.today_timeline("2015-10-16", "2팀").empty


# ---------------------------------------------------------------- ② 달력
def by_id(events):
    return {e["id"]: e for e in events}


def test_calendar_events_colors_done_and_background():
    add_order("LATE3", "2015-10-12", machine_id=8)            # 3일 지남 -> 빨강
    add_order("LATE1", "2015-10-14", machine_id=9)            # 1일 지남 -> 주황
    add_order("URG", DAY, urgent=1, comp="bearing")           # 긴급 -> 빨강
    add_order("TODAY", DAY, start="13:00")                    # 오늘 -> 남색
    add_order("NEXT", "2015-10-16")                           # 미래 -> 남색
    add_order("D1", "2015-10-13", status="DONE")              # 완료만 있는 날 -> "완료 2"
    add_order("D2", "2015-10-13", status="DONE")
    add_order("MIX", "2015-10-12", status="DONE")             # 남은 작업이 있는 날에도 "완료 1"
    add_order("OTHER", "2015-10-12", team="1팀")              # 다른 팀
    add_order("OUT", "2015-11-02")                            # 달 범위 밖
    ev = by_id(ws.calendar_events(DAY, "2팀", *MONTH))
    orders = {k for k, e in ev.items() if e["extendedProps"].get("kind") == "order"}
    assert orders == {"LATE3", "LATE1", "URG", "TODAY", "NEXT"}
    assert ev["LATE3"]["title"] == "#8 모터 교체" and ev["LATE3"]["start"] == "2015-10-12" and ev["LATE3"]["allDay"]
    assert ev["LATE3"]["backgroundColor"] == ev["LATE3"]["borderColor"] == RED
    assert ev["LATE3"]["extendedProps"] == {"kind": "order", "wo_id": "LATE3", "overdue_days": 3, "urgent": False,
                                            "status": "ASSIGNED"}
    assert ev["LATE1"]["backgroundColor"] == ORANGE and ev["LATE1"]["extendedProps"]["overdue_days"] == 1
    assert ev["URG"]["backgroundColor"] == RED and ev["URG"]["title"].startswith("긴급 ")
    assert ev["TODAY"]["backgroundColor"] == NAVY and ev["NEXT"]["backgroundColor"] == NAVY
    assert ev["NEXT"]["extendedProps"]["overdue_days"] == 0
    done = ev["done-2015-10-13"]
    assert done["title"] == "완료 2" and done["backgroundColor"] == GRAY
    assert done["extendedProps"]["kind"] == "done_summary"
    assert ev["done-2015-10-12"]["title"] == "완료 1"
    bg = {e["start"]: e for e in ev.values() if e.get("display") == "background"}
    assert set(bg) == {"2015-10-12", "2015-10-14"}  # 오늘·미래·완료만 있는 날은 배경 없음
    assert bg["2015-10-12"]["backgroundColor"] == ws.BG_RED and bg["2015-10-14"]["backgroundColor"] == ws.BG_ORANGE


def kinds_on(events, day):
    """그 날짜 이벤트 (배경 제외) 제목 목록, order 순."""
    return [e["title"] for e in sorted((e for e in events if e["start"] == day and e.get("display") != "background"),
                                        key=lambda e: (e["order"], e["title"]))]


def test_calendar_urgent_done_stays():
    add_order("U1", "2015-10-14", machine_id=45, comp="bearing", task_type="urgent_check", urgent=1, status="DONE")
    ev = by_id(ws.calendar_events(DAY, "2팀", *MONTH))
    u = ev["U1"]
    assert u["title"] == "긴급 완료 · #45 베어링 긴급 점검" and u["start"] == "2015-10-14"
    assert u["backgroundColor"] == u["borderColor"] == RED_DARK and u["textColor"] == "#FFFFFF"
    assert u["classNames"] == ["ev-urgent-done"]
    assert u["extendedProps"] == {"kind": "order", "wo_id": "U1", "urgent": True, "status": "DONE", "overdue_days": 0}
    assert "done-2015-10-14" not in ev  # 완료한 긴급은 "완료 N"에 섞이지 않는다


def test_calendar_all_done_day_urgent_separate_from_count():
    add_order("U1", "2015-10-13", urgent=1, status="DONE", task_type="urgent_check", comp="bearing", machine_id=45)
    for i in range(3):
        add_order(f"N{i}", "2015-10-13", machine_id=20 + i, status="DONE")
    titles = kinds_on(ws.calendar_events(DAY, "2팀", *MONTH), "2015-10-13")
    assert titles == ["긴급 완료 · #45 베어링 긴급 점검", "완료 3"]


def test_calendar_mixed_day():
    add_order("R1", "2015-10-13", machine_id=30)  # 남은 일반
    add_order("D1", "2015-10-13", machine_id=31, status="DONE")
    add_order("D2", "2015-10-13", machine_id=32, status="DONE")
    add_order("U1", "2015-10-13", machine_id=45, comp="bearing", task_type="urgent_check", urgent=1, status="DONE")
    titles = kinds_on(ws.calendar_events(DAY, "2팀", *MONTH), "2015-10-13")
    assert titles == ["긴급 완료 · #45 베어링 긴급 점검", "#30 모터 교체", "완료 2"]


def test_calendar_done_urgent_only_has_no_overdue_background():
    add_order("U1", "2015-10-10", urgent=1, status="DONE", task_type="urgent_check")
    ev = ws.calendar_events(DAY, "2팀", *MONTH)
    assert [e["id"] for e in ev] == ["U1"] and not any(e.get("display") == "background" for e in ev)


def test_calendar_order_values_and_other_team():
    add_order("RU", "2015-10-14", machine_id=1, urgent=1, task_type="urgent_check")  # 남은 긴급
    add_order("DU", "2015-10-14", machine_id=2, urgent=1, task_type="urgent_check", status="DONE")
    add_order("RN", "2015-10-14", machine_id=3)  # 남은 일반
    add_order("DN", "2015-10-14", machine_id=4, status="DONE")
    add_order("XU", "2015-10-14", machine_id=5, urgent=1, team="1팀", status="DONE")  # 다른 팀 긴급
    ev = by_id(ws.calendar_events(DAY, "2팀", *MONTH))
    assert ev["RU"]["order"] == 0 < ev["DU"]["order"] == 1 < ev["RN"]["order"] == 2 < ev["done-2015-10-14"]["order"] == 3
    assert ev["done-2015-10-14"]["title"] == "완료 1" and "XU" not in ev
    assert ws.calendar_options(DAY)["eventOrder"] == "order,title"


def test_calendar_click_dates_do_not_shift():
    """달력은 timeZone UTC: 날짜 칸 클릭 = 'YYYY-MM-DDT00:00:00.000Z' -> 앞 10자리가 그 날짜 (하루 밀리지 않음)."""
    assert ws.calendar_options(DAY)["timeZone"] == "UTC"
    assert ws.calendar_options(DAY)["now"] == ws.calendar_options(DAY)["initialDate"] == DAY
    assert ws.clicked_date("2015-10-09T00:00:00.000Z") == "2015-10-09"
    assert ws.clicked_date("2015-10-09") == "2015-10-09"  # 하루 종일 이벤트의 start
    assert ws.clicked_date(None) is None


def test_calendar_range_covers_oldest_overdue():
    assert ws.calendar_range(DAY, "2팀") == ("2015-08-01", "2015-11-30")
    add_order("OLD", "2015-06-20")
    assert ws.calendar_range(DAY, "2팀")[0] == "2015-06-20"


def test_default_calendar_date_and_orders_on():
    assert ws.default_calendar_date(DAY, "2팀") == DAY
    add_order("A", "2015-10-09", status="DONE")
    add_order("B", "2015-10-11", start="13:00")
    add_order("C", "2015-10-11", start="09:00", status="DONE")
    add_order("E", "2015-10-11", start="10:00")
    add_order("D", "2015-10-01", team="1팀")
    assert ws.default_calendar_date(DAY, "2팀") == "2015-10-11"
    assert [w["wo_id"] for w in ws.orders_on("2015-10-11", "2팀")] == ["E", "B", "C"]  # 남은 것 먼저, 시작 시각 순
    assert ws.orders_on("2015-10-01", "2팀") == []


# ---------------------------------------------------------------- ③ 내 설비 실시간 센서
def test_my_machines_today_open_plus_urgent():
    add_order("T1", DAY, machine_id=1, start="13:00")
    add_order("T1B", DAY, machine_id=1, start="09:00")                    # 같은 설비: 이른 작업
    add_order("T2", DAY, machine_id=2, status="DONE")                     # 완료 -> 제외
    add_order("U3", "2015-10-12", machine_id=3, urgent=1, start="14:20", task_type="urgent_check")  # 지난 날짜 긴급
    add_order("U1", "2015-10-10", machine_id=1, urgent=1, start="15:00", task_type="urgent_check")  # 설비 1 긴급 우선
    add_order("OLD", "2015-10-12", machine_id=4)                          # 지난 날짜 일반 -> 제외
    add_order("X", DAY, machine_id=5, team="1팀")
    got = ws.my_machines(DAY, "2팀")
    assert [m["machine_id"] for m in got] == [1, 3]
    assert got[0]["wo_id"] == "U1" and got[0]["urgent"] and got[0]["task_label"] == "긴급 점검"
    assert got[1] == {"machine_id": 3, "wo_id": "U3", "comp": "motor", "task_label": "긴급 점검", "start": "14:20",
                      "urgent": True}
    add_order("T6", DAY, machine_id=6)
    assert [m["machine_id"] for m in ws.my_machines(DAY, "2팀")] == [1, 3, 6]


@pytest.fixture
def fake_sensors(monkeypatch):
    """평소값 고정 + 시간 단위 기록 가짜 (원본 pdm.db를 읽지 않음)."""
    base = {"volt": (170.0, 10.0), "rotate": (450.0, 10.0), "pressure": (100.0, 5.0), "vibration": (40.0, 2.0)}
    monkeypatch.setattr("stream.baseline.baseline", lambda m, as_of: base)

    def hourly(machine_id, sensor, hours, as_of):
        end = pd.Timestamp(as_of)
        times = pd.date_range(end=end, periods=hours, freq="h")
        value = {"volt": 172.0, "rotate": 448.0, "pressure": 101.0, "vibration": 47.0}[sensor]  # 진동 z=3.5
        return pd.DataFrame({"datetime": times, "value": [value] * hours})

    monkeypatch.setattr("tools.data_tools._sensor_rows", hourly)
    return base


def test_live_sensor_table_hourly_when_no_stream(fake_sensors):
    add_order("B1", DAY, machine_id=45, comp="bearing", start="14:20", task_type="urgent_check", urgent=1)
    add_stream(45, rotate=300.0)  # 생성기가 멈춰 있으면 스트림 행이 있어도 시간 단위
    df = ws.live_sensor_table(DAY, "2팀", f"{DAY} 06:00:00")
    assert list(df.columns) == ws.SENSOR_COLS and len(df) == 1
    r = df.iloc[0]
    assert r["source"] == "시간 단위" and r["task"] == "긴급 점검 14:20"
    assert r["key_sensor"] == "vibration"  # 베어링 <-> 진동
    assert r["vibration"] == 47.0 and r["key_pct"] == pytest.approx(17.5)
    assert len(r["trend"]) == 24  # 최근 24시간
    assert r["state"] == "경고선 초과"


def test_live_sensor_table_realtime(fake_sensors):
    add_order("D1", DAY, machine_id=7, comp="drive")    # 구동부 <-> 회전수
    add_order("P1", DAY, machine_id=8, comp="pump")     # 펌프 <-> 압력
    add_order("M1", DAY, machine_id=9, comp="motor")    # 모터 <-> 전압 (스트림 오래됨 -> 시간 단위)
    generator_running([7, 8, 9])
    add_stream(7, rotate=400.0)                          # 회전수 z=-5 (아래) -> 경고선 초과
    add_stream(8, pressure=125.0)                        # 압력 z=+5 -> 경고선 초과
    add_stream(9, seconds_ago=30, volt=999.0)            # 5초보다 오래됨
    df = ws.live_sensor_table(DAY, "2팀", f"{DAY} 06:00:00").set_index("machine_id")
    assert df.loc[7, "source"] == "실시간" and df.loc[7, "key_sensor"] == "rotate"
    assert df.loc[7, "rotate"] == 400.0 and df.loc[7, "state"] == "경고선 초과"
    assert df.loc[8, "key_sensor"] == "pressure" and df.loc[8, "state"] == "경고선 초과"
    assert df.loc[9, "source"] == "시간 단위" and df.loc[9, "key_sensor"] == "volt" and df.loc[9, "volt"] == 172.0


def test_sensor_state_rotate_only_down(fake_sensors):
    assert ws.sensor_state("rotate", 400.0, 450.0, 10.0) == "경고선 초과"  # z=-5
    assert ws.sensor_state("rotate", 500.0, 450.0, 10.0) == "주의"         # z=+5: 위로는 경고선 초과가 아님
    assert ws.sensor_state("vibration", 47.0, 40.0, 2.0) == "경고선 초과"
    assert ws.sensor_state("vibration", 44.5, 40.0, 2.0) == "주의"
    assert ws.sensor_state("vibration", 41.0, 40.0, 2.0) == "정상"
    assert ws.sensor_state("volt", None, 170.0, 10.0) == "값 없음"


def test_live_sensor_table_empty():
    df = ws.live_sensor_table(DAY, "2팀", f"{DAY} 06:00:00")
    assert df.empty and list(df.columns) == ws.SENSOR_COLS


# ---------------------------------------------------------------- ④ ⑤ 분포
def test_cause_distribution_order_zero_and_pct():
    add_report("R1", DAY, cause="예측과 같음")
    add_report("R2", "2015-10-14", cause="예측과 같음")
    add_report("R3", "2015-10-13", cause="이상 없음", reported_by=app_db.VIRTUAL_REPORTER)  # 가상 메모도 포함
    df = ws.cause_distribution(DAY, "2팀", "team", 30)
    assert list(df.columns) == ["label", "count", "pct"]
    assert list(df["label"]) == app_db.CAUSE_TYPES and list(df["count"]) == [2, 0, 0, 1]
    assert df["pct"].sum() == pytest.approx(100)
    assert ws.cause_summary_text(df, 30) == "최근 30일 점검 3건 중 2건이 예측과 같았습니다."
    assert ws.cause_summary_text(df, None).startswith("전체 점검 3건")


def test_distribution_time_guard_scope_period():
    add_report("IN", DAY, action="조정·청소")
    add_report("FUTURE", "2015-10-16", action="부품 교체")   # 기준 날짜 이후 -> 제외
    add_report("OTHER", DAY, team="1팀", action="부품 교체")  # 다른 팀
    add_report("OLD31", "2015-09-14", action="센서 재고정")    # 31일 전
    df = ws.action_distribution(DAY, "2팀", "team", 30)
    assert list(df["label"]) == app_db.ACTIONS
    assert dict(zip(df["label"], df["count"])) == {"부품 교체": 0, "조정·청소": 1, "센서 재고정": 0, "임시 조치": 0,
                                                   "조치 없음": 0}
    assert int(ws.action_distribution(DAY, "2팀", "all", 30)["count"].sum()) == 2  # 다른 팀 포함, 미래 제외
    whole = ws.action_distribution(DAY, "2팀", "team", None)
    assert int(whole["count"].sum()) == 2 and whole["pct"].sum() == pytest.approx(100)  # 전체 기간이면 31일 전 포함
    assert ws.action_summary_text(whole) == "가장 많이 한 조치: 조정·청소 1건 (50%)"


def test_distributions_without_reports():
    cause = ws.cause_distribution(DAY, "2팀", "team", 30)
    action = ws.action_distribution(DAY, "2팀", "team", 30)
    assert list(cause["count"]) == [0] * len(app_db.CAUSE_TYPES) and int(action["count"].sum()) == 0
    assert ws.cause_summary_text(cause, 30) == "아직 완료 보고가 없습니다."
    assert ws.action_summary_text(action) == "아직 완료 보고가 없습니다."


# ---------------------------------------------------------------- 작업 이력: 자주 손본 설비 Top 5
def test_top_machines_sort_limit_mode_open():
    for i in range(3):  # #1: 3건 (원인 최빈값 = 다른 부품, 부품 = pump), 마지막 10-03
        add_report(f"M1-{i}", f"2015-10-0{i + 1}", machine_id=1, cause="다른 부품" if i else "예측과 같음",
                   actual_comp="pump")
    add_report("M2-0", "2015-10-05", machine_id=2)
    add_report("M2-1", "2015-10-06", machine_id=2)
    for i, d in enumerate(["2015-10-07", "2015-10-10", "2015-10-09"]):  # #3: 3건, 마지막 10-10
        add_report(f"M3-{i}", d, machine_id=3, actual_comp=None, predicted_comp="bearing")
    for m in (4, 5, 6):
        add_report(f"M{m}", f"2015-10-0{m}", machine_id=m)
    add_order("O1", "2015-10-14", machine_id=2)               # #2 우리 팀 미완료 1건
    add_order("O2", "2015-10-14", machine_id=2, status="DONE")
    add_order("O3", "2015-10-14", machine_id=2, team="1팀")
    df = ws.top_machines(DAY, "2팀", "team", 30)
    assert list(df.columns) == ws.TOP_COLS and len(df) == 5
    assert list(df["machine_id"][:3]) == [3, 1, 2]  # #1·#3 동률(3건) -> 마지막 작업이 최근인 #3 먼저
    assert list(df["work_count"][:3]) == [3, 3, 2]
    t = df.set_index("machine_id")
    assert t.loc[1, "main_cause"] == "다른 부품" and t.loc[1, "main_comp"] == "pump"
    assert t.loc[3, "main_comp"] == "bearing" and t.loc[3, "last_date"] == "2015-10-10"
    assert t.loc[2, "open_count"] == 1 and t.loc[1, "open_count"] == 0


def test_top_machines_fewer_and_empty():
    assert ws.top_machines(DAY, "2팀").empty and list(ws.top_machines(DAY, "2팀").columns) == ws.TOP_COLS
    add_report("A", DAY, machine_id=7)
    assert list(ws.top_machines(DAY, "2팀")["machine_id"]) == [7]


# ---------------------------------------------------------------- 긴급 설비 실시간 자동 포함
def test_stream_add_machine():
    from stream.control import MAX_MACHINES, stream_add_machine

    assert "실행 중이 아니라" in stream_add_machine(45)  # 생성기 정지
    assert app_db.get_state("stream_machines", []) == []
    generator_running([1, 2])
    assert "추가했습니다" in stream_add_machine(45)
    assert app_db.get_state("stream_machines") == [1, 2, 45]
    assert "이미" in stream_add_machine(45) and app_db.get_state("stream_machines") == [1, 2, 45]
    generator_running(list(range(1, MAX_MACHINES + 1)))
    assert "꽉 차" in stream_add_machine(45)
    assert app_db.get_state("stream_machines") == list(range(1, MAX_MACHINES + 1))
