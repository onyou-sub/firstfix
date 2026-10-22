"""3단계 확인: 경고 -> 긴급 점검 바로 배정 -> 조사 에이전트(실제 LLM)로 보강 -> 작업자 완료 보고.

- 실제 운영 기록을 건드리지 않는다: app.db는 임시 파일(APP_DB_PATH), 근거 파일은 임시 evidence 폴더,
  장기 기억은 메모리 저장소, 현장 기록 색인은 임시 Chroma 폴더를 쓴다.
- 배정 시간 겹침·시간당 조사 한도·조사 실패는 가짜 조사 함수로 확인한다 (LLM 없음).
- 실행: uv run python scripts/test_live_alert.py   (.env에 OPENAI_API_KEY 필요)
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

TMP_DIR = Path(tempfile.mkdtemp(prefix="fixfirst_alert_"))
os.environ["APP_DB_PATH"] = str(TMP_DIR / "app.db")
os.environ["URGENT_NOW_HM"] = "09:00"  # 긴급 출동 시각(실제 시계)을 고정해 결과가 실행 시각에 따라 바뀌지 않게

from langgraph.store.memory import InMemoryStore  # noqa: E402

import agents.investigator as investigator  # noqa: E402
import live_ops  # noqa: E402
import memory.store as memory_store  # noqa: E402
import services  # noqa: E402
from store import app_db, field_notes  # noqa: E402
from tools.plan_tools import _hm, load_drafts  # noqa: E402

AS_OF = "2015-11-12 06:00:00"
DAY = AS_OF[:10]
investigator.EVIDENCE_DIR = TMP_DIR / "evidence"
investigator.save_investigation.__globals__["get_store"] = memory_store.get_store
memory_store._STORE = InMemoryStore()
field_notes.CHROMA_DIR = TMP_DIR / "chroma"
field_notes.get_store.cache_clear()
live_ops.RUN_ASYNC = False  # 백그라운드 대신 바로 실행해 결과를 확인

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"\n       {detail}" if detail else ""))
    results.append((name, ok))


def fake_investigate(machine_id, comp, prob, as_of):
    steps = [SimpleNamespace(action="설비 정지·잠금 확인"), SimpleNamespace(action="진동 측정")]
    return SimpleNamespace(label="예측 고장", steps=steps), f"E{as_of[5:7]}{as_of[8:10]}-{machine_id:03d}"


def notes_for(team: str | None = None, role: str = "worker") -> list[dict]:
    return app_db.notifications_for({"role": role, "team": team, "id": "-"}, limit=50)


def overlaps(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return _hm(a[0]) < _hm(b[1]) and _hm(b[0]) < _hm(a[1])


def main() -> None:
    try:
        drafts_before = load_drafts(AS_OF)

        # 1) 실제 조사 에이전트로 끝까지
        out = services.add_scenario(45, "bearing_vibration", AS_OF, created_by="admin")
        a = services.get_alert(out["alerts"][0]["alert_id"])
        check("#45 시나리오 -> 경고 -> 점검 배정", a["level"] == "경고" and a["status"] == "점검 배정",
              f"{a['level']}({a['source']}) 상태 {a['status']} {a['message']}")
        ev = TMP_DIR / "evidence" / "20151112" / "45.json"
        check("근거 파일 evidence/20151112/45.json 생성", ev.exists() and a["evidence_id"] == "E1112-045", str(ev))
        wo = app_db.get_work_order(a["wo_id"] or "")
        check("UWO 작업 지시 ASSIGNED, urgent=1, 자동 승인",
              bool(wo) and wo["wo_id"].startswith("UWO-1112-045-") and wo["status"] == "ASSIGNED"
              and wo["urgent"] == 1 and wo["approved_by"] == live_ops.AUTO_APPROVER,
              f"{wo and (wo['wo_id'], wo['team'], wo['start'], wo['end'], wo['task_type'])}")
        team_notes = [n for n in notes_for(wo["team"]) if n["link_wo_id"] == wo["wo_id"]]
        admin_notes = [n for n in notes_for(role="admin") if n["link_wo_id"] == wo["wo_id"]]
        check("작업자 알림 2건(배정 + 조사 결과 추가됨) + 관리자 배정 알림 1건",
              len(team_notes) == 2 and any("조사 결과 추가됨" in n["title"] for n in team_notes) and len(admin_notes) == 1,
              f"{[n['title'] for n in team_notes + admin_notes]}")
        others = [w for w in app_db.work_orders() if not w["urgent"]]
        check("교체 작업 지시·발주·초안은 생기지 않음",
              not others and not app_db.purchase_orders() and load_drafts(AS_OF) == drafts_before)

        # 2) 작업자 출동 확인 -> 완료 보고
        user = {"id": "worker-test", "role": "worker", "team": wo["team"]}
        services.start_work(wo["wo_id"], user)
        check("작업자 [출동 확인] -> 경고 상태 출동", services.get_alert(a["alert_id"])["status"] == "출동")
        done = services.complete_work(
            wo["wo_id"], user, "예측과 같음", "bearing", "조정·청소", 50, 0,
            "하우징 진동 7.1mm/s 측정, 윤활 부족으로 판단해 그리스 보충 후 재가동 시 진동 4.2mm/s로 내려감. 다음 점검 때 윤활 주기 확인.")
        memory = memory_store.recall(45, AS_OF, include_same_time=True)
        check("완료 보고 -> 경고 점검 완료, 장기 기억·현장 기록 반영",
              services.get_alert(a["alert_id"])["status"] == "점검 완료" and done["indexed"]
              and any(f["text"] == done["sentence"] for m in memory for f in m.get("feedback", [])),
              f"indexed {done['indexed']} {done['index_error'] or ''}")

        # 3) 기존 작업과 겹치지 않음 (가짜 조사, LLM 없음)
        live_ops.INVESTIGATE = fake_investigate
        existing = {}
        for i, team in enumerate(["1팀", "2팀", "3팀"]):
            draft = {"draft_id": f"WO-1112-0{i}1", "as_of": AS_OF, "machine_id": 60 + i, "comp": "motor",
                     "task_type": "replace", "team": team, "start": "09:00", "end": "11:30", "checklist": []}
            app_db.create_work_order(draft, "admin")
            existing[team] = ("09:00", "11:30")
        out = services.add_scenario(12, "bearing_vibration", AS_OF, created_by="admin")
        wo2 = app_db.get_work_order(services.get_alert(out["alerts"][0]["alert_id"])["wo_id"])
        team_orders = [(w["start"], w["end"]) for w in app_db.work_orders(as_of_date=DAY, team=wo2["team"])
                       if w["wo_id"] != wo2["wo_id"] and w["status"] != "DONE"]  # 끝난 작업은 일정 계산에서 빠진다
        check("같은 날 그 팀 남은 작업과 시간이 겹치지 않음 (겹치던 작업은 뒤로 밀림)",
              not any(overlaps((wo2["start"], wo2["end"]), o) for o in team_orders),
              f"{wo2['team']} {wo2['start']}-{wo2['end']} vs {team_orders}")

        # 4) 조사 실패 -> 배정은 유지, "조사 생략" 기록 (경고를 닫지 않음)
        def boom(*_):
            raise RuntimeError("API down")
        live_ops.INVESTIGATE = boom
        out = services.add_scenario(13, "pump_pressure", AS_OF, created_by="admin")
        a = services.get_alert(out["alerts"][0]["alert_id"])
        check("조사 실패 -> 점검 배정 유지 + 조사 생략 기록", a["status"] == "점검 배정" and bool(a["wo_id"])
              and "조사 생략" in a["message"] and "API down" in a["message"] and a["investigated_at"] is None)

        # 5) 시간당 조사 한도 -> 배정은 하고 조사만 생략
        live_ops.INVESTIGATE = fake_investigate
        live_ops.MAX_INVESTIGATIONS_PER_HOUR = live_ops._investigations_last_hour()
        out = services.add_scenario(14, "motor_voltage", AS_OF, created_by="admin")
        a = services.get_alert(out["alerts"][0]["alert_id"])
        check("시간당 조사 한도 초과 -> 배정 + 조사 생략", a["status"] == "점검 배정" and bool(a["wo_id"])
              and "시간당 조사" in a["message"] and a["evidence_id"] is None)
        live_ops.MAX_INVESTIGATIONS_PER_HOUR = 10
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_live_alert.py
