"""긴급 점검 출동 시각과 일정 밀기 (LLM 없음, 임시 app.db·임시 운영 설정).

- 출동 시각 = 기준 날짜 + 지금 실제 시각 (live_ops.NOW_HM을 바꿔 끼워 고정)
- 근무 시간 밖: urgent_off_hours "now"(근무 시간 외 출동) / "next_shift"(근무 시작 전이면 오늘, 후면 다음 날 근무 시작)
- 점심 중이어도 긴급은 바로 시작, 밀린 일반 작업은 점심 뒤로(또는 점심 전후로 나눔)
- ② 진행 중 작업은 중단 후 이어서, ③ 시작 안 한 작업은 뒤로, 이미 잡힌 긴급은 밀지 않음

실행: uv run pytest tests/test_urgent_dispatch.py -v
"""
import pytest

import live_ops
import ops_settings
from store import app_db

DAY = "2015-10-14"
AS_OF = f"{DAY} 06:00:00"
CFG = {**ops_settings.DEFAULTS}  # 09:00~18:00, 점심 12:00~13:00, 준비 30분, 긴급 60분


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(ops_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.delenv("URGENT_NOW_HM", raising=False)


def at(monkeypatch, hm: str, off_hours: str = "now") -> None:
    monkeypatch.setattr(live_ops, "NOW_HM", lambda cfg=None: hm)
    assert ops_settings.save_settings({**ops_settings.DEFAULTS, "urgent_off_hours": off_hours}) == []


def add_wo(wo_id: str, team: str, start: str, end: str, status: str = "ASSIGNED", urgent: int = 0, day: str = DAY):
    with app_db.connect() as conn:
        conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                     " status, started_at, created_at, urgent) VALUES (?, ?, 90, 'motor', 'replace', ?, ?, ?, ?, ?, ?, ?)",
                     (wo_id, day, team, start, end, status, app_db.now() if status == "IN_PROGRESS" else None,
                      app_db.now(), urgent))


def dispatch(machine_id: int = 45, comp: str = "bearing", task: str = "urgent_check") -> dict:
    alert = {"alert_id": f"A-1014-{machine_id:03d}-01", "machine_id": machine_id, "comp": comp, "as_of": AS_OF,
             "message": "vibration 실시간 이상", "level": "경고", "source": "실시간", "evidence_id": None}
    return live_ops.dispatch_urgent(alert, task, ["진동 측정"])


def wo(wo_id: str) -> dict:
    return app_db.get_work_order(wo_id)


# ---------------------------------------------------------------- 요청 시나리오
def test_now_10_starts_urgent_and_pushes_next_job(monkeypatch):
    at(monkeypatch, "10:00")
    add_wo("WO-A", "2팀", "10:30", "12:00")  # 시작 안 한 일반 작업 (베어링 = 2팀 전문)
    out = dispatch()
    assert (out["team"], out["start"], out["end"], out["as_of_date"]) == ("2팀", "10:00", "11:00", DAY)
    # 긴급 끝 11:00 + 준비 30분 = 11:30 시작, 90분 작업이 점심(12:00~13:00)에 걸려 점심 전후로 나눔 -> 14:00 끝
    assert (wo("WO-A")["start"], wo("WO-A")["end"]) == ("11:30", "14:00")
    assert [c["wo_id"] for c in out["changes"]] == ["WO-A"] and "점심 전후로 나눔" in out["changes"][0]["note"]


def test_lunch_urgent_starts_now_pushed_job_after_lunch(monkeypatch):
    at(monkeypatch, "12:20")
    add_wo("WO-B", "2팀", "13:00", "14:30")
    out = dispatch()
    assert (out["start"], out["end"]) == ("12:20", "13:20")  # 점심 중이어도 긴급은 바로
    assert wo("WO-B")["start"] >= "13:00" and (wo("WO-B")["start"], wo("WO-B")["end"]) == ("13:50", "15:20")


def test_after_hours_now_mode_marks_off_hours(monkeypatch):
    at(monkeypatch, "19:10", "now")
    out = dispatch()
    assert (out["as_of_date"], out["start"], out["end"]) == (DAY, "19:10", "20:10") and out["off_hours"]
    assert "근무 시간 외 출동" in out["title"]
    notes = app_db.notifications_for({"role": "worker", "team": "2팀", "id": "w"}, 10)
    assert any("근무 시간 외 출동" in (n["line"] or "") for n in notes if n["kind"] == "urgent")


def test_after_hours_next_shift_goes_to_next_day(monkeypatch):
    at(monkeypatch, "19:10", "next_shift")
    out = dispatch()
    assert (out["as_of_date"], out["start"]) == ("2015-10-15", "09:00") and out["tomorrow"] and not out["off_hours"]
    assert "근무 시간 외 출동" not in out["title"]


def test_before_hours_next_shift_goes_to_today_start(monkeypatch):
    at(monkeypatch, "07:30", "next_shift")
    out = dispatch()
    assert (out["as_of_date"], out["start"]) == (DAY, "09:00") and not out["tomorrow"]


# ---------------------------------------------------------------- 밀기 규칙
def test_in_progress_job_paused_and_resumed_after_urgent(monkeypatch):
    at(monkeypatch, "10:00")
    add_wo("WO-P", "2팀", "09:00", "11:00", status="IN_PROGRESS")  # 10:00에 남은 60분
    add_wo("WO-N", "2팀", "11:30", "12:00")
    out = dispatch()
    assert (out["start"], out["end"]) == ("10:00", "11:00")
    p = wo("WO-P")
    # 11:30부터 남은 60분: 11:30~12:00 + 점심 + 13:00~13:30 -> 13:30 끝 (이어 하는 부분도 점심 전후로 나눔)
    assert (p["start"], p["end"], p["paused"]) == ("09:00", "13:30", "10:00~11:30")
    assert (wo("WO-N")["start"], wo("WO-N")["end"]) == ("14:00", "14:30")  # 그 뒤 + 준비 30분


def test_existing_urgent_is_not_pushed(monkeypatch):
    at(monkeypatch, "10:00")
    add_wo("UWO-OLD", "2팀", "09:30", "10:30", urgent=1)
    out = dispatch()
    assert (out["start"], out["end"]) == ("11:00", "12:00")  # 기존 긴급 끝 + 준비 30분
    assert (wo("UWO-OLD")["start"], wo("UWO-OLD")["end"]) == ("09:30", "10:30")


def test_job_not_overlapping_stays(monkeypatch):
    at(monkeypatch, "10:00")
    add_wo("WO-LATE", "2팀", "15:00", "16:00")
    out = dispatch()
    assert out["changes"] == [] and (wo("WO-LATE")["start"], wo("WO-LATE")["end"]) == ("15:00", "16:00")


def test_other_team_jobs_untouched_and_team_notified(monkeypatch):
    at(monkeypatch, "10:00")
    add_wo("WO-1", "1팀", "10:00", "11:00")
    add_wo("WO-2", "2팀", "10:00", "11:00")
    out = dispatch()
    assert out["team"] == "2팀" and wo("WO-1")["start"] == "10:00" and wo("WO-2")["start"] == "11:30"
    notes = app_db.notifications_for({"role": "worker", "team": "2팀", "id": "w"}, 10)
    assert any(n["title"].startswith("일정 변경: 2팀") for n in notes)


def test_sensor_check_prefers_team_without_interruption(monkeypatch):
    at(monkeypatch, "10:00")
    add_wo("WO-1", "1팀", "09:00", "11:00", status="IN_PROGRESS")
    add_wo("WO-2", "2팀", "10:00", "11:00")
    out = dispatch(machine_id=12, comp="pump", task="sensor_check")  # 공통 작업
    assert out["team"] == "3팀" and out["start"] == "10:00" and out["changes"] == []


# ---------------------------------------------------------------- 출동 시각 함수
def test_now_hm_rounds_up_and_env_override(monkeypatch):
    monkeypatch.setenv("URGENT_NOW_HM", "08:15")
    assert live_ops.now_hm(CFG) == "08:15"
    monkeypatch.delenv("URGENT_NOW_HM")
    hm = live_ops.now_hm(CFG)
    assert len(hm) == 5 and hm[2] == ":"


def test_off_hours_setting_validation():
    assert ops_settings.validate({**ops_settings.DEFAULTS, "urgent_off_hours": "later"})
    assert ops_settings.validate({**ops_settings.DEFAULTS, "urgent_off_hours": "next_shift"}) == []


def test_now_line_only_in_work_hours():
    from datetime import date

    from ui import components as ui

    orders = [{"team": "2팀", "start": "09:00", "end": "10:00", "status": "ASSIGNED", "machine_id": 1, "comp": "motor",
               "wo_id": "W1", "urgent": 0, "paused": "09:30~10:30"}]
    fig = ui.status_gantt(orders, date(2015, 10, 14), CFG)
    assert ui.add_now_line(fig, date(2015, 10, 14), CFG, "10:05")
    assert any(a.text == "지금 10:05" for a in fig.layout.annotations)
    assert "긴급으로 중단 09:30~10:30" in fig.data[0].customdata[0][3]  # 중단 구간은 마우스를 올리면
    fig2 = ui.status_gantt(orders, date(2015, 10, 14), CFG)
    assert not ui.add_now_line(fig2, date(2015, 10, 14), CFG, "19:10")  # 근무 시간 밖이면 그리지 않음
