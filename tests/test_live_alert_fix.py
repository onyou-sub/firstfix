"""긴급 배정이 조사(LLM) 실패에 막히지 않는지 확인 (LLM 없음, 가짜 조사 함수).

- 배정 먼저: 조사가 실패해도 UWO가 생기고 경고는 "점검 배정"
- LLM 크레딧 소진(429 insufficient_quota) -> "조사 생략: LLM 크레딧 소진", 관리자 알림은 한 번만
- 조사 성공 -> 같은 작업 지시에 근거·점검 항목이 붙고 작업자에게 "조사 결과 추가됨"
- 시간당 조사 한도 초과 -> 배정은 되고 조사만 생략 (실패한 조사는 한도에 세지 않음)
- 이전 날짜에 남은 열린 경고가 오늘 경고를 막지 않음, 기준 날짜가 바뀌면 닫힘
- 배정 안 된 채 멈춘 경고를 retry_stuck_alerts가 다시 처리

실행: uv run pytest tests/test_live_alert_fix.py -v
"""

from types import SimpleNamespace

import pandas as pd
import pytest

import live_ops
from store import app_db

AS_OF = "2015-11-12 06:00:00"


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(live_ops, "RUN_ASYNC", False)
    monkeypatch.setattr(live_ops, "_quota_notified", False)
    with app_db.connect():
        pass
    yield


def add_alert(machine_id: int, level: str = "경고", as_of: str = AS_OF, status: str = "열림", comp: str = "bearing",
              created_at: str | None = None) -> str:
    alert_id = f"A-{pd.Timestamp(as_of):%m%d}-{machine_id:03d}-01"
    now = created_at or app_db.now()
    with app_db.connect() as conn:
        conn.execute("INSERT INTO alerts (alert_id, as_of, machine_id, comp, prob, level, source, batch_id, status,"
                     " message, created_at, updated_at) VALUES (?, ?, ?, ?, 0.9, ?, '실시간', 'STREAM-X', ?, ?, ?, ?)",
                     (alert_id, as_of, machine_id, comp, level, status, "vibration 실시간 이상", now, now))
    return alert_id


def ok_investigate(machine_id, comp, prob, as_of):
    steps = [SimpleNamespace(action="진동 측정"), SimpleNamespace(action="윤활 확인")]
    return SimpleNamespace(label="예측 고장", steps=steps), f"E1112-{machine_id:03d}"


def fail(msg):
    def _f(*_):
        raise RuntimeError(msg)
    return _f


def notes(role: str, team: str | None = None) -> list[dict]:
    return app_db.notifications_for({"role": role, "team": team, "id": "-"}, limit=100)


def test_dispatch_even_if_investigation_fails(monkeypatch):
    monkeypatch.setattr(live_ops, "INVESTIGATE", fail("API down"))
    aid = add_alert(45)
    out = live_ops.handle_alert(aid)
    a = live_ops.get_alert(aid)
    wo = app_db.get_work_order(out["wo_id"])
    assert a["status"] == "점검 배정" and a["wo_id"] == wo["wo_id"]
    assert "조사 생략" in a["message"] and "API down" in a["message"]
    assert a["investigated_at"] is None and a["investigating"] == 0  # 실패는 한도에 세지 않음
    assert wo["urgent"] == 1 and wo["checklist"] == live_ops.URGENT_CHECKLIST["bearing"]
    assert any(n["link_wo_id"] == wo["wo_id"] for n in notes("worker", wo["team"]))


def test_quota_error_notifies_admin_once(monkeypatch):
    monkeypatch.setattr(live_ops, "INVESTIGATE", fail("Error code: 429 - insufficient_quota"))
    for m in (11, 12):
        live_ops.handle_alert(add_alert(m))
    a = live_ops.get_alert(f"A-1112-011-01")
    assert a["status"] == "점검 배정" and "LLM 크레딧 소진" in a["message"]
    assert sum("크레딧 소진" in n["title"] for n in notes("admin")) == 1


def test_success_enriches_same_work_order(monkeypatch):
    monkeypatch.setattr(live_ops, "INVESTIGATE", ok_investigate)
    aid = add_alert(45)
    out = live_ops.handle_alert(aid)
    wo = app_db.get_work_order(out["wo_id"])
    assert out["investigated"] and live_ops.get_alert(aid)["evidence_id"] == "E1112-045"
    assert wo["evidence_id"] == "E1112-045" and wo["checklist"] == ["진동 측정", "윤활 확인"]
    assert any("조사 결과 추가됨" in n["title"] for n in notes("worker", wo["team"]))


def test_sensor_finding_switches_task(monkeypatch):
    from tools.plan_tools import SENSOR_LABEL

    monkeypatch.setattr(live_ops, "INVESTIGATE",
                        lambda *a: (SimpleNamespace(label=SENSOR_LABEL, steps=[SimpleNamespace(action="센서 확인")]), "E1"))
    out = live_ops.handle_alert(add_alert(45))
    assert app_db.get_work_order(out["wo_id"])["task_type"] == "sensor_check"


def test_human_check_is_sensor_check(monkeypatch):
    monkeypatch.setattr(live_ops, "INVESTIGATE", fail("x"))
    out = live_ops.handle_alert(add_alert(45, level="사람 확인"))
    wo = app_db.get_work_order(out["wo_id"])
    assert wo["task_type"] == "sensor_check" and wo["checklist"] == live_ops.SENSOR_CHECKLIST


def test_hourly_limit_skips_investigation_only(monkeypatch):
    called = []
    monkeypatch.setattr(live_ops, "INVESTIGATE", lambda *a: called.append(a) or ok_investigate(*a))
    monkeypatch.setattr(live_ops, "MAX_INVESTIGATIONS_PER_HOUR", 0)
    aid = add_alert(45)
    out = live_ops.handle_alert(aid)
    a = live_ops.get_alert(aid)
    assert out["wo_id"] and a["status"] == "점검 배정" and "시간당 조사" in a["message"] and not called


def test_handle_twice_creates_one_work_order(monkeypatch):
    monkeypatch.setattr(live_ops, "INVESTIGATE", fail("x"))
    aid = add_alert(45)
    live_ops.handle_alert(aid)
    again = live_ops.handle_alert(aid)
    assert again.get("skipped") and len([w for w in app_db.work_orders() if w["urgent"]]) == 1


def test_previous_day_open_alert_does_not_block_today():
    old = add_alert(45, as_of="2015-11-10 06:00:00")
    assert live_ops._open_alert(45)["alert_id"] == old  # 날짜를 안 주면 예전처럼 보임
    assert live_ops._open_alert(45, AS_OF) is None  # 오늘 기준으로는 열린 경고 없음
    assert live_ops.close_stale_alerts(AS_OF) == 1
    assert live_ops.get_alert(old)["status"] == "종료"


def test_assigned_previous_day_alert_is_kept(monkeypatch):
    old = add_alert(45, as_of="2015-11-10 06:00:00", status="점검 배정")
    assert live_ops.close_stale_alerts(AS_OF) == 0 and live_ops.get_alert(old)["status"] == "점검 배정"


def test_retry_stuck_alerts(monkeypatch):
    monkeypatch.setattr(live_ops, "INVESTIGATE", fail("x"))
    past = (pd.Timestamp(app_db.now()) - pd.Timedelta(seconds=60)).isoformat(timespec="seconds")
    stuck = add_alert(45, status="대기", created_at=past)
    fresh = add_alert(46)  # 방금 생긴 경고는 아직 다시 처리하지 않음
    ids = live_ops.retry_stuck_alerts(AS_OF)
    assert ids == [stuck]
    assert live_ops.get_alert(stuck)["status"] == "점검 배정" and live_ops.get_alert(fresh)["status"] == "열림"


def test_progress_text():
    assert live_ops.alert_progress({"status": "점검 배정", "team": "2팀", "start": "09:00", "investigating": 1}) \
        == "2팀 09:00 | 출동 확인 대기 · 조사 중"
    assert live_ops.alert_progress({"status": "열림", "level": "경고"}) == "배정 중"
