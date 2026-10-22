"""계획 에이전트 확인 (실제 OpenAI API 호출).

- 각 날짜의 고위험 설비 중 evidence가 없는 설비는 먼저 run_fanout으로 조사
- 케이스 1: 2015-10-01 고위험 5대 -> 하루 일정에 5건 (교체 3 + 임시 점검 2), 작업 지시서 5건(팀·시간 포함),
  재고 0 부품 발주 초안
- 케이스 2: 2015-12-22 -> 13번 "사람 확인"(센서 점검 1시간 일정), 13번 작업 지시서 없음
- 케이스 3: 2015-10-06 고위험 0대 -> 배정 없음, 초안 0건, "오늘 정비 대상 없음"
- 같은 날짜 두 번 실행 -> 초안 개수 그대로 (중복 방지)
- 발주 누락 방지: 재고 0 부품(임시 점검 작업)이 있는 날 발주 초안이 반드시 1건 이상,
  발주 초안을 지운 뒤 코드 검사(_ensure_purchase_drafts)를 돌리면 auto_filled=true로 다시 생김
"""

import json
import sys
import time
from pathlib import Path

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.fanout import run_fanout  # noqa: E402
from agents.planner import _ensure_purchase_drafts, plan  # noqa: E402
from ml.predict import predict_df  # noqa: E402
from tools.plan_tools import (  # noqa: E402
    DRAFTS_DIR, HUMAN_CHECK, _drafts_path, is_assigned, load_drafts, load_findings, plan_table,
)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def ensure_evidence(as_of: str) -> None:
    pred = predict_df(as_of)
    high = sorted(int(m) for m in pred.loc[pred["high_risk"], "machine_id"])
    have = {f["machine_id"] for f in load_findings(as_of)}
    missing = [m for m in high if m not in have]
    print(f"고위험 {high}, evidence 있음 {sorted(have)}" + (f", 조사 실행 {missing}" if missing else ""))
    if missing:
        t0 = time.perf_counter()
        out = run_fanout(missing, as_of)
        print(f"  조사 완료 {time.perf_counter() - t0:.1f}s: " + ", ".join(f"#{r['machine_id']}={r['status']}" for r in out))


def reset_drafts(as_of: str) -> None:
    """이전 테스트에서 만든 이 날짜 초안 파일을 지우고 시작 (이 스크립트가 만든 출력물만 대상)."""
    path = DRAFTS_DIR / f"{pd.Timestamp(as_of):%Y%m%d}.json"
    if path.exists():
        path.unlink()


def show(out: dict, elapsed: float) -> None:
    print(f"\n[배정표] ({elapsed:.1f}s)")
    print(plan_table(out["plan"]))
    print("\n[초안]")
    for d in out["drafts"]:
        target = f"#{d['machine_id']} {d['comp']} {d['slot']}" if d["type"] == "work_order" else f"{d['comp']} {d['qty']}개"
        print(f"  {d['draft_id']:<16} {d['status']:<8} {target:<22} 근거 {','.join(d['evidence_ids'])}"
              f"{'  (auto_filled)' if d.get('auto_filled') else ''}")
        if d["type"] == "work_order":
            print(f"    체크리스트: {d['checklist']}")
    if not out["drafts"]:
        print("  (없음)")
    print(f"\nauto_filled: {out['auto_filled'] or '없음'}")
    print("\n[에이전트 답변]\n" + out["answer"])


def run_plan(as_of: str) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = plan(as_of)
    return out, time.perf_counter() - t0


def case1() -> None:
    as_of = "2015-10-01 06:00:00"
    header(f"케이스 1: {as_of} (고위험 5대)")
    ensure_evidence(as_of)
    reset_drafts(as_of)
    out, elapsed = run_plan(as_of)
    show(out, elapsed)

    rows = out["plan"]
    assigned = [r for r in rows if is_assigned(r["slot"])]
    wos = [d for d in out["drafts"] if d["type"] == "work_order"]
    prs = [d for d in out["drafts"] if d["type"] == "purchase_request"]
    check("케이스 1: 5건 모두 하루 일정에 배치", len([r for r in rows if r.get("team")]) == 5,
          ", ".join(f"#{r['machine_id']}={r['slot']}" for r in rows))
    check("케이스 1: 교체 3 + 임시 점검 2", sorted(r["task_type"] for r in rows) ==
          sorted(["replace"] * 3 + ["temporary_check"] * 2), str([r["task_type"] for r in rows]))
    check("케이스 1: 작업 지시서 5건 (일정의 교체·임시 점검과 일치)",
          sorted(d["machine_id"] for d in wos) == sorted(r["machine_id"] for r in assigned), f"{len(wos)}건")
    by_m = {r["machine_id"]: r for r in rows}
    check("케이스 1: 작업 지시서에 팀·시작·종료·작업 종류", all(
        (d["team"], d["start"], d["end"], d["task_type"]) ==
        (by_m[d["machine_id"]]["team"], by_m[d["machine_id"]]["start"], by_m[d["machine_id"]]["end"],
         by_m[d["machine_id"]]["task_type"]) for d in wos))
    check("케이스 1: 체크리스트 첫 항목이 정지 절차", all(d["checklist"][0] == "점검 전 설비 정지 절차 확인" for d in wos))
    no_stock = sorted({r["comp"] for r in assigned if r.get("needs_purchase")})
    check("케이스 1: 재고 0 부품 발주 초안", not no_stock or {d["comp"] for d in prs} >= set(no_stock),
          f"재고 0 배정 부품 {no_stock}, 발주 {[d['draft_id'] for d in prs]}")
    temp_comps = sorted({r["comp"] for r in rows if r.get("task_type") == "temporary_check"})
    check("발주 누락 방지: 재고 0 부품이 있는 날 발주 초안 1건 이상", not temp_comps or len(prs) >= 1,
          f"재고 0 부품 {temp_comps}, 발주 {[d['draft_id'] for d in prs]}")
    stocked = [d["draft_id"] for d in prs if d["current_qty"] > 0]
    check("케이스 1: 재고 있는 부품 발주 없음", not stocked, f"재고 있는데 발주: {stocked}" if stocked else "")

    print("\n같은 날짜 다시 실행 (중복 방지 확인)...")
    before = sorted(d["draft_id"] for d in out["drafts"])
    out2, elapsed2 = run_plan(as_of)
    after = sorted(d["draft_id"] for d in out2["drafts"])
    print(f"  {elapsed2:.1f}s, 1회차 {len(before)}건 -> 2회차 {len(after)}건, auto_filled {out2['auto_filled'] or '없음'}")
    check("중복 방지: 두 번 실행해도 초안 개수 그대로", len(before) == len(after) and before == after,
          f"{len(before)}건 -> {len(after)}건")

    print("\n발주 초안을 지우고 코드 검사 실행 (에이전트가 발주를 빠뜨린 상황)...")
    path = _drafts_path(as_of)
    original = path.read_bytes()
    try:
        drafts = json.loads(original)
        removed = [k for k, d in drafts.items() if d["type"] == "purchase_request"]
        for k in removed:
            del drafts[k]
        path.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
        filled = _ensure_purchase_drafts(as_of)
        refilled = {k: d for k, d in load_drafts(as_of).items() if d["type"] == "purchase_request"}
        check("발주 누락 방지: 지운 발주 초안을 코드가 다시 만듦",
              bool(removed) and sorted(filled) == sorted(removed) == sorted(refilled), f"지움 {removed} -> 생성 {filled}")
        check("발주 누락 방지: 코드가 만든 초안은 auto_filled=true",
              bool(refilled) and all(d["auto_filled"] is True for d in refilled.values()))
        check("발주 누락 방지: 희망 발주일 기본값 = 기준 날짜",
              all(d["order_date"] == as_of[:10] for d in refilled.values()))
    finally:
        path.write_bytes(original)


def case2() -> None:
    as_of = "2015-12-22 06:00:00"
    header(f"케이스 2: {as_of} (13번 motor 교체 직후)")
    ensure_evidence(as_of)
    reset_drafts(as_of)
    out, elapsed = run_plan(as_of)
    show(out, elapsed)

    row13 = next((r for r in out["plan"] if r["machine_id"] == 13), None)
    check("케이스 2: 13번 '사람 확인' 센서 점검 1시간 일정", row13 is not None and row13["slot"].startswith(HUMAN_CHECK)
          and row13["task_type"] == "sensor_check" and row13["duration_min"] == 60, f"slot={row13 and row13['slot']}")
    check("케이스 2: 13번 작업 지시서 없음", not any(d.get("machine_id") == 13 for d in out["drafts"]))


def case3() -> None:
    as_of = "2015-10-06 06:00:00"
    header(f"케이스 3: {as_of} (고위험 0대)")
    ensure_evidence(as_of)
    reset_drafts(as_of)
    try:
        out, elapsed = run_plan(as_of)
    except Exception as e:
        check("케이스 3: 에러 없이 실행", False, f"{type(e).__name__}: {e}")
        return
    show(out, elapsed)
    check("케이스 3: 배정 없음", out["plan"] == [])
    check("케이스 3: 초안 0건", out["drafts"] == [])
    check("케이스 3: '오늘 정비 대상 없음' 답변", "오늘 정비 대상 없음" in out["answer"])


def main() -> None:
    case1()
    case2()
    case3()

    header("요약")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    print(f"초안 파일: {DRAFTS_DIR.relative_to(ROOT_DIR)}, 배정표: outputs/plans/")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_planner.py
