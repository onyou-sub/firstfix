"""재고 0 부품 보충 발주 (LLM 없음, 임시 운영 DB·임시 결과 폴더).

- 오늘 작업에 필요 없어도 재고가 0이면 발주 초안 (auto_filled, 메모, 관리자 알림)
- 승인된 발주가 입고 대기 중이면 중복 발주하지 않음, 입고 예정일이 지나면 다시 발주
- 작업자가 마지막 부품을 쓰면 관리자 알림
"""

import pytest

import services
from agents import planner
from store import app_db
from tools import plan_tools

AS_OF = "2015-10-06 06:00:00"  # 고위험 0대인 날. 기본 재고: pump 0, motor 1, drive 4, bearing 3


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(plan_tools, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(plan_tools, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(plan_tools, "EVIDENCE_DIR", tmp_path / "evidence")


def approved_po(comp: str, as_of: str, order_date: str | None = None) -> None:
    draft = {"draft_id": f"PR-{as_of[5:7]}{as_of[8:10]}-{comp}", "as_of": as_of, "comp": comp, "qty": 1,
             "order_date": order_date}
    assert app_db.create_purchase_order(draft, "admin")


def test_zero_stock_without_task_gets_restock_draft():
    filled = planner._ensure_purchase_drafts(AS_OF)
    assert filled == ["PR-1006-pump"]  # 재고 0은 pump뿐 (오늘 작업 없음)
    d = plan_tools.load_drafts(AS_OF)["PR-1006-pump"]
    assert d["status"] == "PENDING" and d["auto_filled"] is True and "보충" in d["note"] and d["for_machines"] == []
    assert any(n["title"].startswith("재고 0 발주 승인 대기: pump") for n in app_db.notifications_for({"role": "admin"}))
    assert planner._ensure_purchase_drafts(AS_OF) == []  # 두 번 돌려도 중복 없음


def test_in_transit_order_blocks_duplicate_then_overdue_reorders():
    approved_po("pump", "2015-10-05 06:00:00")  # 10-05 발주, 입고 3일 -> 10-08 예정
    assert planner._ensure_purchase_drafts(AS_OF) == []  # 10-06: 입고 대기 중
    z = {x["comp"]: x for x in plan_tools.zero_stock_parts(AS_OF)}
    assert z["pump"]["in_transit"]["expected_arrival"] == "2015-10-08"
    late = "2015-10-09 06:00:00"  # 입고 예정일이 지났는데 입고 처리 안 됨 -> 재고 0 그대로 -> 다시 발주
    assert planner._ensure_purchase_drafts(late) == ["PR-1009-pump"]
    assert [o["po_id"] for o in plan_tools.zero_stock_parts(late)[0]["overdue"]] == ["PR-1005-pump"]


def test_received_order_means_no_reorder():
    approved_po("pump", "2015-10-02 06:00:00")
    assert app_db.receive_purchase_order("PR-1002-pump", "2015-10-02 18:00:00")  # 입고 -> 재고 +1
    assert planner._ensure_purchase_drafts(AS_OF) == []
    assert all(x["comp"] != "pump" for x in plan_tools.zero_stock_parts(AS_OF))


def test_dashboard_status_lines():
    approved_po("pump", "2015-10-05 06:00:00")
    lines = {a["comp"]: a for a in services.stock_alerts(AS_OF)}
    assert lines["pump"]["level"] == "info" and "입고 예정 2015-10-08" in lines["pump"]["status"]


def test_worker_uses_last_part_alerts_admin(monkeypatch):
    monkeypatch.setattr("services._save_feedback", lambda *a, **k: None)
    monkeypatch.setattr("services.field_notes.index_report", lambda wo, report: None)
    draft = {"draft_id": "WO-1001-090", "type": "work_order", "as_of": "2015-10-01 06:00:00", "machine_id": 90,
             "comp": "motor", "task_type": "replace", "team": "2팀", "start": "09:00", "end": "11:00",
             "checklist": ["점검 전 설비 정지 절차 확인"], "evidence_ids": ["E1001-090"]}
    app_db.create_work_order(draft, "admin")
    worker = {"id": "team2", "role": "worker", "team": "2팀"}
    services.start_work("WO-1001-090", worker)
    out = services.complete_work("WO-1001-090", worker, "예측과 같음", "motor", "부품 교체", 95, 1,
                                 "단자대 그을림, 입력 전압 190V로 높았음. 모터 교체 후 171V로 정상.")
    assert out["stock_out"] is True  # motor 기본 재고 1 -> 0
    titles = [n["title"] for n in app_db.notifications_for({"role": "admin"})]
    assert any(t.startswith("재고 0: 모터/전원부") for t in titles)
    assert planner._ensure_purchase_drafts("2015-10-02 06:00:00") == ["PR-1002-motor", "PR-1002-pump"]
