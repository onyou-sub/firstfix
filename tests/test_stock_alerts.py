"""재고 0 부품 대시보드 카드: 조치가 필요한 것만, [확인]하면 같은 상태인 동안 숨김 (LLM 없음, 임시 운영 DB·결과 폴더).

- 발주 요청 없음 -> 보임, [확인] 후 숨김, 같은 상태면 계속 숨김
- 승인 대기 -> 보임 / 승인 후 발주 진행 중 -> 카드에서 빠지고 "발주 진행 중 1건"
- 확인한 뒤 상태가 거절로 바뀌면 다시 보임
- 입고 예정일 지남 -> 발주 진행 중이어도 보임
- 발주가 진행 중이면 마지막 부품을 써도 재고 0 알림을 새로 만들지 않음

실행: uv run pytest tests/test_stock_alerts.py -v
"""

import pytest

import services
from agents import planner
from store import app_db
from tools import plan_tools

AS_OF = "2015-10-06 06:00:00"  # 고위험 0대인 날. 기본 재고: pump 0, motor 1, drive 4, bearing 3 (pump 입고 3일)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(plan_tools, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(plan_tools, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(plan_tools, "EVIDENCE_DIR", tmp_path / "evidence")


def pump(as_of: str = AS_OF) -> dict:
    return next(a for a in services.stock_alerts(as_of) if a["comp"] == "pump")


def shown(as_of: str = AS_OF) -> list[str]:
    return [a["comp"] for a in services.stock_card(as_of)["show"]]


def approved_po(as_of: str) -> str:
    po_id = f"PR-{as_of[5:7]}{as_of[8:10]}-pump"
    assert app_db.create_purchase_order({"draft_id": po_id, "as_of": as_of, "comp": "pump", "qty": 1}, "admin")
    return po_id


def test_no_request_shown_then_acked_stays_hidden():
    a = pump()
    assert (a["key"], a["action_needed"], a["level"]) == ("none", True, "error") and shown() == ["pump"]
    services.ack_stock_alert("pump", a["key"], by="admin")
    assert shown() == [] and pump()["acked"]
    assert shown() == []  # 같은 상태면 계속 숨김


def test_pending_then_approved_moves_to_in_progress_line():
    assert planner._ensure_purchase_drafts(AS_OF) == ["PR-1006-pump"]
    a = pump()
    assert (a["key"], a["action_needed"], a["level"]) == ("pending:PR-1006-pump", True, "warning")
    assert shown() == ["pump"]
    plan_tools.set_draft_status(AS_OF, "PR-1006-pump", "APPROVED")  # 오늘 승인 -> 발주 진행 중
    card = services.stock_card(AS_OF)
    assert pump()["key"] == "approved:PR-1006-pump" and card == {"show": [], "in_progress": 1}
    approved_po(AS_OF)  # 다음 날에는 입고 대기 중 발주
    nxt = "2015-10-07 06:00:00"
    assert pump(nxt)["key"] == "in_transit:PR-1006-pump" and services.stock_card(nxt) == {"show": [], "in_progress": 1}


def test_acked_state_change_shows_again():
    planner._ensure_purchase_drafts(AS_OF)
    services.ack_stock_alert("pump", pump()["key"], by="admin")
    assert shown() == []
    plan_tools.set_draft_status(AS_OF, "PR-1006-pump", "REJECTED")  # 승인 대기 -> 거절
    a = pump()
    assert (a["key"], a["level"], a["acked"]) == ("rejected:PR-1006-pump", "error", False) and shown() == ["pump"]


def test_overdue_shown_even_while_in_transit():
    late = approved_po("2015-10-01 06:00:00")  # 입고 예정 10-04, 처리 안 됨
    on_way = approved_po("2015-10-05 06:00:00")  # 입고 예정 10-08 (진행 중)
    a = pump()
    assert a["key"] == f"in_transit:{on_way}|overdue:{late}"
    assert a["action_needed"] and a["level"] == "warning" and "입고 처리 확인" in a["status"]
    assert shown() == ["pump"]
    services.ack_stock_alert("pump", a["key"], by="admin")
    assert shown() == [] and services.stock_card(AS_OF)["in_progress"] == 0


def test_no_new_stock_out_notice_while_order_in_transit(monkeypatch):
    monkeypatch.setattr("services._save_feedback", lambda *a, **k: None)
    monkeypatch.setattr("services.field_notes.index_report", lambda wo, report: None)
    po = {"draft_id": "PR-0930-motor", "as_of": "2015-09-30 06:00:00", "comp": "motor", "qty": 1}
    assert app_db.create_purchase_order(po, "admin")  # motor 발주 진행 중 (입고 예정일 전)
    draft = {"draft_id": "WO-1001-090", "type": "work_order", "as_of": "2015-10-01 06:00:00", "machine_id": 90,
             "comp": "motor", "task_type": "replace", "team": "2팀", "start": "09:00", "end": "11:00",
             "checklist": ["점검 전 설비 정지 절차 확인"], "evidence_ids": ["E1001-090"]}
    app_db.create_work_order(draft, "admin")
    worker = {"id": "team2", "role": "worker", "team": "2팀"}
    services.start_work("WO-1001-090", worker)
    services.complete_work("WO-1001-090", worker, "예측과 같음", "motor", "부품 교체", 95, 1, 
                           "단자대 그을림, 입력 전압 190V로 높았음. 모터 교체 후 171V로 정상.")
    titles = [n["title"] for n in app_db.notifications_for({"role": "admin", "id": "admin"}, 50)]
    assert not any(t.startswith("재고 0:") for t in titles)  # 이미 발주가 진행 중이라 새 알림 없음
