"""폐쇄 루프 확인: 승인 → 작업 지시·알림 → 작업자 완료 보고 → 다음 날 조사·재고에 반영 (실제 OpenAI API 호출).

- C1: 2015-10-01 점검 → 관리자 전부 승인 → work_orders 생성(ASSIGNED), 해당 팀 알림, 발주·관리자 알림
- C2: 2팀 작업자가 교체 작업 1건 [작업 시작] → [완료 보고](예측과 같음, 부품 교체, 사용 부품 1, 측정값을 담은 상세 메모)
      → work_reports 1건(메모), maint_log 1건, stock_ledger -1, 관리자 알림, 현장 기록 색인(field_notes).
      짧은 메모(30자 미만)·다른 팀은 거부
- C3: 2015-10-02 기준 같은 설비를 investigate_one → 기억에 작업자 보고 문장, 정비 이력에 "(작업자 보고)" 교체,
      최근 7일 교체 규칙도 작업자 보고 교체를 본다. check_parts_stock은 사용 부품만큼 줄어든다.
      field_notes 검색(search_field_notes)에 측정값이 나온다
- C4: 2015-09-30 기준 기억·정비 이력·재고 → 10-01 보고가 보이지 않음 (미래 차단)
- C5: 다른 팀 작업자는 시작·완료 처리 모두 거부
"""

import shutil
import sys
import time
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

import services  # noqa: E402
from agents.investigator import _recent_replacement, investigate_one  # noqa: E402
from store import app_db, field_notes  # noqa: E402
from tools.data_tools import _maint_rows, maintenance_history_text  # noqa: E402
from tools.memory_tools import memory_text  # noqa: E402
from tools.plan_tools import _stock, load_drafts  # noqa: E402

AS_OF, NEXT, BEFORE = "2015-10-01 06:00:00", "2015-10-02 06:00:00", "2015-09-30 06:00:00"
NOON = "2015-10-01 12:00:00"
ADMIN = {"id": "admin", "role": "admin", "team": None}
WORKER2 = {"id": "team2", "role": "worker", "team": "2팀"}
WORKER1 = {"id": "team1", "role": "worker", "team": "1팀"}
MEMO = "하우징 온도 높았음"
MEASURE = "단자대 온도 68도"  # 메모에 적은 측정값 -> 다음 조사의 현장 기록 검색에 나와야 함
LONG_MEMO = (f"{MEMO}. {MEASURE}로 평소보다 높고 그을림이 있었음. 입력 전압 190V. 모터 교체 후 재가동 전압 171V로 정상."
             " 다음 점검 때 단자 체결 토크와 단자대 온도를 먼저 확인할 것.")
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def raises(fn, exc) -> tuple[bool, str]:
    try:
        fn()
    except exc as e:
        return True, f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"다른 예외 {type(e).__name__}: {e}"
    return False, "예외 없음"


def notes(role: str, team: str | None = None) -> list[dict]:
    return app_db.notifications_for({"id": "-", "role": role, "team": team}, limit=100)


def c1() -> dict:
    header("C1: 점검 → 전부 승인 → 작업 지시·알림 생성")
    services.reset_day_outputs(AS_OF)
    app_db.clear_day(AS_OF)
    field_notes.delete_day(AS_OF)
    tid = f"loop-{uuid.uuid4().hex[:6]}"
    t0 = time.perf_counter()
    services.start_daily_check(AS_OF, tid)
    while services.get_job(tid)["running"]:
        time.sleep(0.5)
    job = services.get_job(tid)
    result = job["result"] or {}
    print(f"점검 완료 ({time.perf_counter() - t0:.1f}s): status={result.get('status')}, 오류={job['error']}")
    check("C1: 승인 대기", result.get("status") == "pending", str([r["draft_id"] for r in result.get("requests", [])]))
    if result.get("status") != "pending":
        return {}
    done = services.submit_decisions(tid, AS_OF, ["approve"] * len(result["requests"]), decided_by="admin")
    print(f"전부 승인 -> {done['status']}")

    drafts = load_drafts(AS_OF)
    approved_wo = sorted(k for k, d in drafts.items() if d["type"] == "work_order" and d["status"] == "APPROVED")
    approved_pr = sorted(k for k, d in drafts.items() if d["type"] == "purchase_request" and d["status"] == "APPROVED")
    orders = app_db.work_orders(AS_OF)
    check("C1: 승인한 작업 지시서마다 work_orders(ASSIGNED)", bool(approved_wo)
          and sorted(w["wo_id"] for w in orders) == approved_wo and all(w["status"] == "ASSIGNED" for w in orders),
          f"{[(w['wo_id'], w['team'], w['status']) for w in orders]}")
    check("C1: 승인자 = admin", all(w["approved_by"] == "admin" for w in orders))
    team_notes = {t: [n for n in notes("worker", t) if n["as_of_date"] == "2015-10-01"] for t in {w["team"] for w in orders}}
    check("C1: 팀마다 '새 작업' 알림", all(len(team_notes[w["team"]]) and any(n["link_wo_id"] == w["wo_id"]
                                                                       for n in team_notes[w["team"]]) for w in orders),
          "; ".join(n["title"] for ns in team_notes.values() for n in ns[:1]))
    check("C1: 다른 팀 알림은 보이지 않음", all(n["to_team"] == "1팀" for n in notes("worker", "1팀")
                                         if n["to_role"] == "worker"))
    pos = app_db.purchase_orders(AS_OF)
    check("C1: 승인한 발주마다 purchase_orders + 관리자 알림",
          sorted(p["po_id"] for p in pos) == approved_pr
          and all(any(p["po_id"] in (n["body"] or "") for n in notes("admin")) for p in pos), str(approved_pr))
    before = len(app_db.work_orders(AS_OF))
    app_db.create_work_order(drafts[approved_wo[0]], "admin")
    check("C1: 같은 초안 두 번 등록해도 작업 지시 1건", len(app_db.work_orders(AS_OF)) == before)
    return {w["wo_id"]: w for w in orders}


def c2(orders: dict) -> dict | None:
    header("C2: 2팀 작업자 작업 시작 → 완료 보고")
    wo = next((w for w in orders.values() if w["team"] == "2팀" and w["task_type"] == "replace"), None)
    if wo is None:
        check("C2: 2팀 교체 작업 있음", False, str([(w["wo_id"], w["team"]) for w in orders.values()]))
        return None
    print(f"대상: {wo['wo_id']} {wo['title']} ({wo['team']} {wo['start']}~{wo['end']})")
    stock_before = _stock(NEXT)[wo["comp"]]["qty"]

    ok, msg = raises(lambda: services.start_work(wo["wo_id"], WORKER1), PermissionError)
    check("C5: 다른 팀(1팀) 작업 시작 거부", ok, msg)
    services.start_work(wo["wo_id"], WORKER2)
    check("C2: 작업 시작 → IN_PROGRESS", app_db.get_work_order(wo["wo_id"])["status"] == "IN_PROGRESS")

    args = dict(actual_cause_type="예측과 같음", actual_comp=wo["comp"], action="부품 교체", duration_min=95,
                parts_used=1, deviation="")
    ok, msg = raises(lambda: services.complete_work(wo["wo_id"], WORKER2, memo=MEMO, **args), ValueError)
    check("C2: 짧은 메모(30자 미만)면 제출 거부", ok and "30자" in msg, msg)
    ok, msg = raises(lambda: services.complete_work(wo["wo_id"], WORKER1, memo=LONG_MEMO, **args), PermissionError)
    check("C5: 다른 팀(1팀) 완료 보고 거부", ok, msg)
    check("C2: 거부된 동안 상태 그대로 (IN_PROGRESS)", app_db.get_work_order(wo["wo_id"])["status"] == "IN_PROGRESS")

    out = services.complete_work(wo["wo_id"], WORKER2, memo=LONG_MEMO, **args)
    print(f"완료 보고 문장: {out['sentence']}")
    reports = [r for r in app_db.work_reports() if r["wo_id"] == wo["wo_id"]]
    maint = [m for m in app_db.maint_log() if m["wo_id"] == wo["wo_id"]]
    ledger = [s for s in app_db.stock_ledger() if s["ref_id"] == wo["wo_id"]]
    admin_notes = [x for x in notes("admin") if x["link_wo_id"] == wo["wo_id"] and x["title"].startswith("완료 보고")]
    check("C2: work_reports 1건", len(reports) == 1)
    check("C2: work_reports에 상세 메모 저장", bool(reports) and reports[0]["memo"] == LONG_MEMO)
    check("C2: 현장 기록 색인 성공", out["indexed"], out.get("index_error") or "")
    check("C2: 장기 기억 문장에 메모 핵심(측정값 포함)", MEMO in out["sentence"] and MEASURE in out["sentence"],
          out["sentence"])
    check("C2: work_orders DONE", app_db.get_work_order(wo["wo_id"])["status"] == "DONE")
    check("C2: maint_log 1건 (기준 날짜 + 종료 시각)", len(maint) == 1 and maint[0]["datetime"] == f"2015-10-01 {wo['end']}:00",
          str(maint))
    check("C2: stock_ledger -1", [s["delta"] for s in ledger] == [-1], str(ledger))
    check("C2: 관리자 알림 '완료 보고'", len(admin_notes) == 1, admin_notes[0]["title"] if admin_notes else "-")
    check("C2: 재고 반영 (10-02 기준 -1)", _stock(NEXT)[wo["comp"]]["qty"] == max(0, stock_before - 1),
          f"{stock_before} -> {_stock(NEXT)[wo['comp']]['qty']}")
    ok, msg = raises(lambda: services.complete_work(wo["wo_id"], WORKER2, memo=LONG_MEMO, **args), ValueError)
    check("C2: 같은 작업 두 번 완료 보고 거부", ok, msg)
    return {**wo, "sentence": out["sentence"]}


def c3_c4(wo: dict) -> None:
    m, comp = wo["machine_id"], wo["comp"]
    header(f"C3: 2015-10-02 기준 #{m} 다시 조사 → 작업자 보고 반영")
    mem = memory_text(m, NEXT)
    hist = maintenance_history_text(m, NEXT)
    rule = _recent_replacement(m, comp, NOON)  # 원본 데이터의 10-02 06:00 교체(실제 고장 기록)보다 앞선 시각
    merged = _maint_rows(m, NEXT)
    print(f"[recall_machine_memory 10-02]\n{mem}\n\n[get_maintenance_history 10-02]\n{hist}\n\n"
          f"[최근 교체 규칙 10-01 12:00] {rule}")
    check("C3: 기억에 작업자 보고 문장", "작업자 보고(2팀): 예측과 같음" in mem and MEMO in mem)
    check("C3: 정비 이력에 '(작업자 보고)' 교체", "(작업자 보고)" in hist)
    check("C3: 합친 교체 기록(_maint_rows)에 작업자 보고 행",
          ((merged["source"] == "작업자 보고") & (merged["comp"] == comp)).sum() == 1)
    check("C3: 최근 7일 교체 규칙이 작업자 보고 교체를 봄 (10-01 12:00 기준)",
          rule["recent"] and rule["last_replaced"].startswith(f"2015-10-01 {wo['end']}"), str(rule))
    notes_m = field_notes.search_text("단자대 온도 전압", NEXT, machine_id=m)
    notes_any = field_notes.search_text("모터 단자대 과열", NEXT)
    print(f"\n[search_field_notes 10-02, #{m}]\n{notes_m}")
    check("C3: 현장 기록 검색(이 설비)에 측정값", "68도" in notes_m)
    check("C3: 현장 기록 검색(전체 설비)에도 이 보고", wo["wo_id"] in notes_any)
    check("C3: 현장 기록에 상세 메모 전체", "단자 체결 토크" in notes_m)

    evidence = ROOT_DIR / "evidence" / "20151002" / f"{m}.json"
    backup = evidence.with_suffix(".json.bak")
    if evidence.exists():
        shutil.copy(evidence, backup)
    try:
        t0 = time.perf_counter()
        try:
            finding, eid = investigate_one(m, comp, 0.9, NEXT)
        except Exception as e:  # LLM 구조화 출력이 가끔 틀림 (실제 흐름은 fanout이 설비별로 잡는다) -> 한 번 재시도
            print(f"investigate_one 실패, 재시도: {type(e).__name__}")
            finding, eid = investigate_one(m, comp, 0.9, NEXT)
        print(f"\ninvestigate_one #{m} ({time.perf_counter() - t0:.1f}s) -> {eid}: {finding.label}\n  {finding.summary}")
        import json

        ev = json.loads(evidence.read_text(encoding="utf-8"))
        outputs = {c["name"]: c.get("output") or "" for c in ev.get("tool_calls", [])}
        print(f"  호출한 도구: {list(outputs)}")
        check("C3: 조사 중 recall_machine_memory 결과에 작업자 보고", "작업자 보고(2팀)" in outputs.get("recall_machine_memory", ""),
              "호출 안 함" if "recall_machine_memory" not in outputs else "")
        if "search_field_notes" in outputs:
            check("C3: 조사 중 search_field_notes 결과에 측정값", "68도" in outputs["search_field_notes"])
        else:
            print("  (이번 조사에서는 search_field_notes를 호출하지 않음 - 도구 단위 검색은 위에서 확인)")
        if "get_maintenance_history" in outputs:
            check("C3: 조사 중 get_maintenance_history에 (작업자 보고)", "(작업자 보고)" in outputs["get_maintenance_history"])
        check("C3: 조사 규칙 기록(rule_check) = 최근 교체", bool((ev.get("rule_check") or {}).get("recent")),
              str(ev.get("rule_check")))
    finally:
        if backup.exists():
            shutil.move(backup, evidence)  # 다른 화면·테스트가 쓰는 10-02 근거 파일 원복

    header("C4: 2015-09-30 기준 → 10-01 보고가 보이지 않음 (미래 차단)")
    mem_b, hist_b = memory_text(m, BEFORE), maintenance_history_text(m, BEFORE)
    check("C4: 기억에 작업자 보고 없음", "작업자 보고" not in mem_b)
    check("C4: 정비 이력에 작업자 보고 없음", "작업자 보고" not in hist_b)
    check("C4: 같은 날 06시 기준에도 11시 교체는 없음", "(작업자 보고)" not in maintenance_history_text(m, AS_OF))
    check("C4: 09-30 재고에 사용 기록 미반영", _stock(BEFORE)[comp]["qty"] == _stock()[comp]["qty"])
    check("C4: 09-30 기준 현장 기록 검색 없음", "68도" not in field_notes.search_text("단자대 온도", BEFORE, machine_id=m))
    check("C4: 같은 날 06시 기준에도 11시 현장 기록은 없음",
          "68도" not in field_notes.search_text("단자대 온도", AS_OF, machine_id=m))


def main() -> None:
    t0 = time.perf_counter()
    orders = c1()
    wo = c2(orders) if orders else None
    if wo:
        c3_c4(wo)
    header(f"요약 (전체 {time.perf_counter() - t0:.0f}s)")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if results and all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_closed_loop.py
