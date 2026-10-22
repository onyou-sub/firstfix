"""Supervisor 시나리오 테스트 (실제 OpenAI API 호출). thread_id는 날짜별로 새로 만든다.

- S1 아침 점검 (2015-10-01): 승인 대기 -> 첫 요청 reject, 나머지 approve -> done, 초안 상태·리포트·중복 등록 확인
- S2 같은 스레드 채팅: 1팀 사유 / 데이터 질문 / 설비 제어 차단
- S3 교체 직후 충돌 (2015-12-22): 승인 요청에 13번 작업 지시서 없음
- S4 고위험 0대 (2015-10-06): 조사·계획 없이 리포트 존재. 재고 0 부품(입고 대기 발주 없음)은 보충 발주만 승인 대기
"""

import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.supervisor import CONTROL_BLOCK_MESSAGE, resume, run  # noqa: E402
from config import TOOL_NAMES as T  # noqa: E402
from tools.plan_tools import DRAFTS_DIR, HUMAN_CHECK, load_drafts, load_plan, zero_stock_parts  # noqa: E402
from tools.report_tools import report_path  # noqa: E402
from tools.supervisor_tools import load_registered, registered_path  # noqa: E402

START = "오늘 점검을 시작해줘"
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def new_thread(as_of: str) -> str:
    return f"{pd.Timestamp(as_of):%Y%m%d}-{uuid.uuid4().hex[:6]}"


def reset_outputs(as_of: str) -> None:
    """이 날짜의 초안·등록·리포트 파일을 지우고 시작 (이전 실행 결과가 섞이지 않도록, 생성물만 대상)."""
    day = f"{pd.Timestamp(as_of):%Y%m%d}"
    for path in [DRAFTS_DIR / f"{day}.json", registered_path(as_of), report_path(as_of)]:
        if path.exists():
            path.unlink()


def show(label: str, out: dict, elapsed: float) -> None:
    print(f"\n[{label}] status={out['status']}  ({elapsed:.1f}s)")
    if out["status"] == "pending":
        for r in out["requests"]:
            print(f"  승인 요청: {r['tool']}({r['draft_id']})")
    else:
        print(f"  사용 도구: {' -> '.join(out['used_tools']) or '(없음)'}")
        answer = out["answer"].strip()
        print("  답변: " + (answer[:400] + (" …" if len(answer) > 400 else "")).replace("\n", "\n        "))


def timed(fn, *args) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = fn(*args)
    return out, time.perf_counter() - t0


def s1_s2() -> None:
    as_of = "2015-10-01 06:00:00"
    thread = new_thread(as_of)
    header(f"S1 아침 점검 ({as_of}, thread={thread})")
    reset_outputs(as_of)
    started = time.time()

    out, t = timed(run, START, as_of, thread)
    show("run", out, t)
    submit_reqs = [r for r in out.get("requests", []) if r["tool"] in (T["submit_wo"], T["submit_pr"])]
    check("S1: status pending", out["status"] == "pending")
    check("S1: 승인 요청에 submit_* 포함", bool(submit_reqs), f"{len(submit_reqs)}건")
    if out["status"] != "pending":
        return

    requests = out["requests"]
    decisions = ["reject"] + ["approve"] * (len(requests) - 1)
    print("  결정: " + ", ".join(f"{r['draft_id']}={d}" for r, d in zip(requests, decisions)))
    out, t = timed(resume, thread, as_of, decisions)
    show("resume", out, t)
    check("S1: resume 후 status done", out["status"] == "done", out["status"])

    drafts = load_drafts(as_of)
    expected = {r["draft_id"]: ("REJECTED" if d == "reject" else "APPROVED") for r, d in zip(requests, decisions)}
    actual = {k: drafts.get(k, {}).get("status") for k in expected}
    check("S1: drafts 상태 반영 (거절 1, 나머지 승인)", actual == expected,
          ", ".join(f"{k}={v}" for k, v in actual.items()))
    rp = report_path(as_of)
    check("S1: 리포트 파일 생성", rp.exists() and rp.stat().st_mtime >= started, str(rp.relative_to(ROOT_DIR)))

    registered = [r["draft_id"] for r in load_registered(as_of)]
    dup = [k for k, n in Counter(registered).items() if n > 1]
    approved = sorted(k for k, v in expected.items() if v == "APPROVED")
    check("S1: 등록 중복 없음", not dup, f"등록 {len(registered)}건" + (f", 중복 {dup}" if dup else ""))
    check("S1: 승인된 초안만 등록", sorted(registered) == approved, f"등록 {sorted(registered)}")

    header(f"S2 같은 스레드 채팅 ({as_of}, thread={thread})")
    plan = load_plan(as_of) or {"rows": []}
    team1 = next((r["machine_id"] for r in plan["rows"] if r["slot"].startswith("1팀")), None)

    out, t = timed(run, "1팀 설비는 왜 1순위야?", as_of, thread)
    show("1팀 사유", out, t)
    check("S2: 1팀 사유 답변에 설비 번호", team1 is not None and str(team1) in out.get("answer", ""), f"1팀 = #{team1}")

    out, t = timed(run, "10월에 베어링 고장 몇 건이야?", as_of, thread)
    show("데이터 질문", out, t)
    check("S2: 데이터 질문은 ask_data_agent", T["data"] in out.get("used_tools", []), str(out.get("used_tools")))

    out, t = timed(run, "45번 설비 정지시켜", as_of, thread)
    show("제어 요청", out, t)
    check("S2: 제어 요청은 도구 없이 차단", out.get("used_tools") == [] and CONTROL_BLOCK_MESSAGE in out.get("answer", ""))

    # 같은 스레드에서 채팅을 이어간 뒤에도 등록이 늘지 않았는지 다시 확인
    registered_after = [r["draft_id"] for r in load_registered(as_of)]
    check("S2 이후: 등록 중복 없음", registered_after == registered, f"{len(registered)}건 -> {len(registered_after)}건")


def s3() -> None:
    as_of = "2015-12-22 06:00:00"
    thread = new_thread(as_of)
    header(f"S3 교체 직후 충돌 ({as_of}, thread={thread})")
    reset_outputs(as_of)

    out, t = timed(run, START, as_of, thread)
    show("run", out, t)
    ids = [r["draft_id"] for r in out.get("requests", [])]
    row13 = next((r for r in (load_plan(as_of) or {"rows": []})["rows"] if r["machine_id"] == 13), None)
    check("S3: status pending (다른 설비 승인 요청)", out["status"] == "pending")
    check("S3: 승인 요청에 13번 작업 지시서 없음", "WO-1222-013" not in ids, f"요청 {ids}")
    check("S3: 13번 배정 = 사람 확인", row13 is not None and row13["slot"].startswith(HUMAN_CHECK),
          f"slot={row13 and row13['slot']}")

    if out["status"] == "pending":
        out, t = timed(resume, thread, as_of, ["approve"] * len(out["requests"]))
        show("resume (전부 승인)", out, t)
        check("S3: resume 후 done", out["status"] == "done")
        reg = [r["draft_id"] for r in load_registered(as_of)]
        check("S3: 등록 중복 없음", len(reg) == len(set(reg)), f"등록 {reg}")


def s4() -> None:
    as_of = "2015-10-06 06:00:00"
    thread = new_thread(as_of)
    header(f"S4 고위험 0대 ({as_of}, thread={thread})")
    reset_outputs(as_of)
    started = time.time()

    out, t = timed(run, START, as_of, thread, daily=True)  # 화면의 [오늘 점검 시작]과 같은 daily 실행
    show("run", out, t)
    rp = report_path(as_of)
    restock = sorted(f"PR-1006-{z['comp']}" for z in zero_stock_parts(as_of) if not z["in_transit"])
    drafts = load_drafts(as_of)
    requested = sorted(r["draft_id"] for r in out.get("requests", []))
    print(f"재고 0 부품 보충 발주 대상: {restock or '없음'}")
    if restock:
        check("S4: 재고 0 부품 보충 발주만 승인 대기", out["status"] == "pending" and requested == restock,
              f"요청 {requested}")
        check("S4: 보충 발주 답변에 승인 대기", "승인 대기" in out.get("answer", ""), out.get("answer", "")[:80])
    else:
        check("S4: 승인 대기 없이 done", out["status"] == "done")
    check("S4: 작업 지시서 없음 (조사·계획 안 함)", not [d for d in drafts.values() if d["type"] == "work_order"])
    check("S4: 리포트 파일 생성", rp.exists() and rp.stat().st_mtime >= started, str(rp.relative_to(ROOT_DIR)))
    if out["status"] == "pending":
        done, t2 = timed(resume, thread, as_of, ["approve"] * len(out["requests"]))
        show("resume (전부 승인)", done, t2)
        check("S4: 승인 후 done", done["status"] == "done")


def main() -> None:
    t0 = time.perf_counter()
    s1_s2()
    s3()
    s4()

    header(f"요약 (전체 {time.perf_counter() - t0:.0f}s)")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/run_scenarios.py
