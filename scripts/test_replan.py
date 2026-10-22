"""거절 사유를 반영한 재계획 확인 (2015-10-01, 실제 OpenAI API 호출).

- R1: 점검 → 승인 대기 → "2팀 오늘 교육이라 불가" → 새 배정에 2팀 없음, v1 초안 SUPERSEDED → 전부 승인 → v2만 등록, 리포트에 재계획 이력
- R2: 새 스레드 → "가장 위험한 설비는 생산 일정 때문에 오늘 정비 불가" → 그 설비 "제외(사유)",
  다음 순위 설비가 그 자리(같은 팀 첫 작업 09:00)를 이어받음
- R3: (R2 스레드) "오늘은 정비 인력이 아무도 없음" → 배정 0건, 새 작업 지시서 0건, 재고 0 부품 발주만 승인 대기
- R4: (R2 스레드) 재계획 3회째까지 진행 후 4번째 요청 → "재계획 한도 초과"
"""

import json
import sys
import time
import uuid
from pathlib import Path

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

import services  # noqa: E402
from agents.planner import MAX_REPLANS, replan  # noqa: E402
from tools.plan_tools import is_assigned, load_drafts, load_plan, slot_team  # noqa: E402
from tools.report_tools import report_path  # noqa: E402
from tools.supervisor_tools import load_registered  # noqa: E402

AS_OF = "2015-10-01 06:00:00"
results: list[tuple[str, bool]] = []
pd.set_option("display.width", 200)
pd.set_option("display.unicode.east_asian_width", True)


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def thread() -> str:
    return f"replan-{uuid.uuid4().hex[:6]}"


def start(tid: str) -> dict:
    t0 = time.perf_counter()
    services.start_daily_check(AS_OF, tid)
    while services.get_job(tid)["running"]:
        time.sleep(0.5)
    job = services.get_job(tid)
    print(f"점검 완료 ({time.perf_counter() - t0:.1f}s): status={job['result'] and job['result']['status']}"
          f", 요청 {[r['draft_id'] for r in (job['result'] or {}).get('requests', [])]}"
          + (f", 오류 {job['error']}" if job["error"] else ""))
    return job["result"] or {}


def do_replan(tid: str, reason: str) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = services.request_replan(tid, AS_OF, reason)
    elapsed = time.perf_counter() - t0
    plan = load_plan(AS_OF)
    print(f"\n재계획 요청: \"{reason}\"  ({elapsed:.1f}s) -> status={out['status']}, 배정안 v{plan['version']}")
    print(f"해석된 PlanConstraints: {json.dumps(plan.get('constraints'), ensure_ascii=False)}")
    if plan.get("diff"):
        diff = pd.DataFrame(plan["diff"]).rename(columns={"machine_id": "설비", "comp": "부품", "before": "이전 배정",
                                                         "after": "새 배정", "changed": "변경"})
        print(diff.to_string(index=False))
    if out["status"] == "pending":
        print(f"새 승인 요청: {[r['draft_id'] for r in out['requests']]}")
    else:
        print("Supervisor 답변: " + out["answer"].strip().replace("\n", "\n  ")[:600])
    return out, elapsed


def r1() -> None:
    header("R1: 2팀 불가 → 2팀 없는 배정안 v2, v1 초안 대체, v2만 등록")
    tid = thread()
    first = start(tid)
    check("R1: 첫 점검 승인 대기", first.get("status") == "pending")
    v1_ids = [r["draft_id"] for r in first.get("requests", [])]

    out, _ = do_replan(tid, "2팀 오늘 교육이라 불가")
    plan = load_plan(AS_OF)
    teams = [slot_team(r["slot"]) for r in plan["rows"] if is_assigned(r["slot"])]
    drafts = load_drafts(AS_OF)
    check("R1: 다시 승인 대기", out["status"] == "pending")
    check("R1: 배정안 v2", plan["version"] == 2)
    check("R1: 새 배정에 2팀 없음", "2팀" not in teams and bool(teams), f"배정 팀 {teams}")
    check("R1: v1 초안은 SUPERSEDED", all(drafts[i]["status"] == "SUPERSEDED" for i in v1_ids),
          ", ".join(f"{i}={drafts[i]['status']}" for i in v1_ids))
    new_ids = [r["draft_id"] for r in out.get("requests", [])]
    check("R1: 새 승인 요청은 모두 -v2 초안", bool(new_ids) and all(i.endswith("-v2") for i in new_ids), str(new_ids))

    if out["status"] == "pending":
        t0 = time.perf_counter()
        done = services.submit_decisions(tid, AS_OF, ["approve"] * len(out["requests"]))
        print(f"\n전부 승인 ({time.perf_counter() - t0:.1f}s) -> {done['status']}: {done.get('answer', '')[:200]}")
        registered = [r["draft_id"] for r in load_registered(AS_OF)]
        check("R1: registered에는 v2 초안만", bool(registered) and all(i.endswith("-v2") for i in registered),
              str(registered))
        report = report_path(AS_OF).read_text(encoding="utf-8") if report_path(AS_OF).exists() else ""
        check("R1: 리포트에 재계획 이력", "재계획 이력" in report)


def r2_r3_r4() -> None:
    header("R2: 가장 위험한 설비 제외 → 다음 순위 설비 배정")
    tid = thread()
    start(tid)
    v1 = load_plan(AS_OF)
    top = v1["rows"][0]["machine_id"]
    top_team = v1["rows"][0]["team"]
    next_up = v1["rows"][1]["machine_id"]
    print(f"v1 1순위 설비 #{top} ({v1['rows'][0]['slot']}), 2순위 #{next_up} ({v1['rows'][1]['slot']})")

    out, _ = do_replan(tid, "가장 위험한 설비는 생산 일정 때문에 오늘 정비 불가")
    rows = {r["machine_id"]: r for r in load_plan(AS_OF)["rows"]}
    check("R2: 1순위 설비가 '제외(사유)'", rows[top]["slot"].startswith("제외("), f"#{top} {rows[top]['slot']}")
    check("R2: 1순위 설비는 팀·시간 없음", rows[top]["team"] is None and rows[top]["start"] is None)
    check("R2: 2순위 설비가 09:00 첫 작업으로 배정", is_assigned(rows[next_up]["slot"]) and rows[next_up]["start"] == "09:00",
          f"#{next_up} {rows[next_up]['slot']} (v1 1순위 팀 {top_team})")

    header("R3: 정비 인력 없음 → 배정 0건, 작업 지시서 0건, 재고 0 부품 발주만")
    try:
        out, _ = do_replan(tid, "오늘은 정비 인력이 아무도 없음")
        plan = load_plan(AS_OF)
        new_drafts = [d for d in load_drafts(AS_OF).values() if d.get("version") == plan["version"]]
        check("R3: 에러 없이 처리", True)
        check("R3: 배정 0건", not any(is_assigned(r["slot"]) for r in plan["rows"]), f"v{plan['version']}")
        # 일정에 못 들어가도(내일 우선) 재고 0 부품의 발주는 리드타임 때문에 오늘 올린다 -> 새 초안은 발주만
        temp_comps = sorted({r["comp"] for r in plan["rows"] if r.get("task_type") == "temporary_check"})
        check("R3: 새 작업 지시서 0건", not [d for d in new_drafts if d["type"] == "work_order"],
              f"{[d['draft_id'] for d in new_drafts]}")
        check("R3: 재고 0 부품 발주 초안만", sorted(d["comp"] for d in new_drafts) == temp_comps,
              f"재고 0 부품 {temp_comps}, 새 초안 {[d['draft_id'] for d in new_drafts]}")
        requested = [r["draft_id"] for r in out.get("requests", [])]
        check("R3: 승인 대기는 발주만 (없으면 안내로 끝)",
              (out["status"] == "pending" and requested and all(i.startswith("PR-") for i in requested))
              or (out["status"] == "done" and not temp_comps and bool(out.get("answer"))), f"{out['status']} {requested}")
    except Exception as e:
        check("R3: 에러 없이 처리", False, f"{type(e).__name__}: {e}")

    header(f"R4: 재계획 {MAX_REPLANS}회 후 4번째 요청 → 한도 초과")
    do_replan(tid, "1팀만 가능")
    v_before = load_plan(AS_OF)["version"]
    print(f"현재 배정안 v{v_before} (재계획 {v_before - 1}회)")
    out, _ = do_replan(tid, "3팀만 가능")
    v_after = load_plan(AS_OF)["version"]
    answer = out.get("answer", "")
    check("R4: 4번째 요청 후 버전 그대로", v_before == v_after == MAX_REPLANS + 1, f"v{v_before} -> v{v_after}")
    check("R4: Supervisor 답변에 한도 초과 안내", "한도" in answer, answer[:120].replace("\n", " "))
    direct = replan(AS_OF, "2팀만 가능")
    check("R4: replan() 직접 호출도 '재계획 한도 초과'", direct["answer"].startswith("재계획 한도 초과"), direct["answer"][:80])


def main() -> None:
    t0 = time.perf_counter()
    r1()
    r2_r3_r4()
    header(f"요약 (전체 {time.perf_counter() - t0:.0f}s)")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_replan.py
