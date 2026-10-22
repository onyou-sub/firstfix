"""채팅 발주 요청과 희망 발주일 확인 (2015-10-01, pump 재고 0, 실제 OpenAI API 호출).

- P1: "내일 발주 넣을 수 있게 요청 올려줘" → order_date = as_of 다음 날, 답변에 부품·수량·희망 발주일·승인 대기,
      승인 카드에 "리드타임 3일 → 예상 입고 MM-DD", 승인하면 app.db purchase_orders.order_date도 같은 날짜
- P2: "발주 요청 올려줘" → order_date = as_of 날짜, 답변에 오늘 요청을 권장하는 이유
- P3: "다음 주 월요일에 발주하도록 요청 올려줘" → order_date = 2015-10-05 (2015-10-01은 목요일)
"""

import sys
import time
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

import services  # noqa: E402
from store import app_db  # noqa: E402
from tools.plan_tools import load_drafts  # noqa: E402

AS_OF = "2015-10-01 06:00:00"
PR_ID = "PR-1001-pump"
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def ask(message: str) -> tuple[str, dict, dict | None]:
    """그날 결과를 비우고 새 스레드에서 채팅 1회 -> (thread_id, 답변, 발주 초안)."""
    services.reset_day_outputs(AS_OF)
    app_db.clear_day(AS_OF)
    tid = f"pr-{uuid.uuid4().hex[:6]}"
    t0 = time.perf_counter()
    out = services.chat(tid, AS_OF, message)
    print(f"\n\"{message}\" ({time.perf_counter() - t0:.1f}s) -> {out['status']}"
          f", 요청 {[r['draft_id'] for r in out.get('requests', [])]}\n답변:\n{out['answer']}")
    draft = load_drafts(AS_OF).get(PR_ID)
    if draft:
        print(f"초안: 희망 발주일 {draft.get('order_date')}, 수량 {draft['qty']}, 메모 {draft.get('note')!r}")
    return tid, out, draft


def p1() -> None:
    header("P1: 내일 발주")
    tid, out, d = ask("내일 발주 넣을 수 있게 요청 올려줘")
    check("P1: 승인 대기 (발주 등록 요청)", out["status"] == "pending"
          and [r["draft_id"] for r in out["requests"]] == [PR_ID], str(out.get("requests")))
    check("P1: order_date = 2015-10-02", bool(d) and d["order_date"] == "2015-10-02", d and d["order_date"])
    check("P1: 답변에 부품·수량·희망 발주일·승인 대기",
          all(w in out["answer"] for w in ["pump", f"{d['qty'] if d else '?'}개", "희망 발주일 2015-10-02", "승인 대기"]))
    card = next((x for x in services.load_day(AS_OF)["drafts"] if x["id"] == PR_ID), {})
    check("P1: 승인 카드에 예상 입고 안내", card.get("notice") == "리드타임 3일 → 예상 입고 10-05", card.get("notice"))
    check("P1: 승인 카드 본문에 희망 발주일", "희망 발주일 2015-10-02" in card.get("body", ""))
    if out["status"] == "pending":
        services.submit_decisions(tid, AS_OF, ["approve"] * len(out["requests"]), decided_by="admin")
        po = next((p for p in app_db.purchase_orders(AS_OF) if p["po_id"] == PR_ID), None)
        check("P1: 승인 후 purchase_orders.order_date = 2015-10-02", bool(po) and po["order_date"] == "2015-10-02",
              str(po))


def p2() -> None:
    header("P2: 날짜 없이 발주 요청")
    _, out, d = ask("발주 요청 올려줘")
    check("P2: 승인 대기는 발주 1건만 (점검·계획을 새로 돌리지 않음)", out["status"] == "pending"
          and [r["draft_id"] for r in out["requests"]] == [PR_ID], str([r["draft_id"] for r in out.get("requests", [])]))
    check("P2: order_date = 2015-10-01 (오늘)", bool(d) and d["order_date"] == "2015-10-01", d and d["order_date"])
    check("P2: 답변에 희망 발주일 오늘·승인 대기", "희망 발주일 2015-10-01" in out["answer"] and "승인 대기" in out["answer"])
    check("P2: 답변에 오늘 요청 권장 이유 (리드타임)", "권장" in out["answer"] and "리드타임" in out["answer"])
    card = next((x for x in services.load_day(AS_OF)["drafts"] if x["id"] == PR_ID), {})
    check("P2: 오늘 발주면 예상 입고 안내 없음", not card.get("notice"))


def p3() -> None:
    header("P3: 다음 주 월요일")
    _, out, d = ask("다음 주 월요일에 발주하도록 요청 올려줘")
    check("P3: order_date = 2015-10-05", bool(d) and d["order_date"] == "2015-10-05", d and d["order_date"])
    check("P3: 답변에 희망 발주일 2015-10-05", "희망 발주일 2015-10-05" in out["answer"])
    check("P3: 날짜를 말했으면 메모에 '오늘 요청 권장' 없음", bool(d) and "권장" not in (d.get("note") or ""),
          d and d.get("note"))


def main() -> None:
    t0 = time.perf_counter()
    try:
        p1()
        p2()
        p3()
    finally:
        services.reset_day_outputs(AS_OF)  # 테스트용 발주만 남은 상태를 지운다 (점검을 다시 실행하면 새로 생김)
        app_db.clear_day(AS_OF)
    header(f"요약 (전체 {time.perf_counter() - t0:.0f}s)")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if results and all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_purchase_request.py
