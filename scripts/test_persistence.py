"""재시작 후 상태 유지 확인 (실제 OpenAI API 호출). 서버 재시작 대신 프로세스를 두 번 띄운다.

① 첫 프로세스: 2015-10-01 오늘 점검 -> 승인 대기로 멈춘 채 종료, 고위험 설비 1대에 save_feedback
② 두 번째 프로세스: 같은 thread_id로 resume(approve) 성공, recall에 피드백 존재, 채팅 기록이 이어짐

체크포인트·장기 기억은 임시 폴더(FIXFIRST_STATE_DIR)에 저장해 data/checkpoints.db·memory.db는 건드리지 않는다.
2015-10-01 결과 파일과 운영 DB 기록(작업 지시·발주)은 다른 시나리오 스크립트처럼 지우고 다시 만든다.

실행: uv run python scripts/test_persistence.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

AS_OF = "2015-10-01 06:00:00"
NEXT_DAY = "2015-10-02 06:00:00"
USER = "persist-test"  # sessions 테이블 확인용 사용자
FEEDBACK = "현장 확인 결과 센서 고정 볼트 풀림이 원인이었음 (재시작 테스트)"
QUESTION = "방금 점검에서 고위험 설비는 몇 대였고, 승인 결과는 어땠어?"

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}", flush=True)


def finish(out_path: Path, extra: dict | None = None) -> None:
    """자식 프로세스: 결과를 파일로 남긴다 (부모가 합산)."""
    data = {"results": results, **(extra or {})}
    out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- ① 첫 프로세스
def first(thread_id: str, out_path: Path) -> None:
    import services
    from agents.supervisor import DAILY_MESSAGE, build_supervisor, run
    from memory.store import save_feedback
    from store import app_db
    from tools.plan_tools import load_findings

    header(f"① 첫 프로세스 (pid {os.getpid()}): {AS_OF[:10]} 점검 -> 승인 대기")
    services.reset_day_outputs(AS_OF)
    app_db.clear_day(AS_OF)
    check("sessions에 thread_id 저장", app_db.session_thread(USER, AS_OF, thread_id) == thread_id)

    t0 = time.perf_counter()
    result = run(DAILY_MESSAGE, AS_OF, thread_id, daily=True)
    print(f"({time.perf_counter() - t0:.1f}s) status={result['status']}"
          f" requests={[r['draft_id'] for r in result.get('requests', [])]}")
    check("승인 대기 상태로 멈춤", result["status"] == "pending" and bool(result["requests"]))

    findings = load_findings(AS_OF)
    machine_id = int(findings[0]["machine_id"]) if findings else 1
    save_feedback(machine_id, AS_OF, FEEDBACK, "eng01")
    print(f"#{machine_id} 피드백 저장: {FEEDBACK}")

    messages = build_supervisor().get_state({"configurable": {"thread_id": thread_id}}).values["messages"]
    finish(out_path, {"machine_id": machine_id, "requests": result.get("requests", []),
                      "message_ids": [m.id for m in messages]})
    print("승인하지 않고 프로세스 종료", flush=True)


# ---------------------------------------------------------------- ② 두 번째 프로세스
def second(thread_id: str, out_path: Path, before: dict) -> None:
    import services
    from agents.supervisor import build_supervisor, pending_requests, resume
    from memory.store import recall
    from store import app_db

    header(f"② 두 번째 프로세스 (pid {os.getpid()}): 같은 thread_id로 이어서")
    check("sessions에서 같은 thread_id를 찾음",
          app_db.session_thread(USER, AS_OF, f"new-{uuid.uuid4()}") == thread_id)

    config = {"configurable": {"thread_id": thread_id}}
    restored = build_supervisor().get_state(config).values.get("messages", [])
    check("첫 프로세스의 대화 기록이 체크포인트에서 복원됨",
          [m.id for m in restored] == before["message_ids"], f"{len(restored)}개 메시지")
    pending = pending_requests(thread_id)
    check("승인 대기 요청이 그대로 남아 있음", pending == before["requests"], str([r["draft_id"] for r in pending]))

    # 장기 기억: 다음 날 기준으로는 보이고, 같은 시각 기준(조사용)으로는 보이지 않는다
    machine_id = before["machine_id"]
    seen = recall(machine_id, NEXT_DAY)
    check(f"{NEXT_DAY[:10]} 기준 recall에 #{machine_id} 피드백 존재",
          any(f["text"] == FEEDBACK for r in seen for f in r.get("feedback", [])))
    check("기준 시각 이전 기억만 읽기 유지 (같은 시각 recall엔 없음)",
          not any(f["text"] == FEEDBACK for r in recall(machine_id, AS_OF) for f in r.get("feedback", [])))

    t0 = time.perf_counter()
    out = resume(thread_id, AS_OF, ["approve"] * len(pending), engineer_id="admin")
    print(f"resume ({time.perf_counter() - t0:.1f}s) status={out['status']}\n답변: {out.get('answer', '')[:300]}")
    check("resume(approve) 성공 (승인 대기 해소)", out["status"] == "done" and not pending_requests(thread_id))
    wo_ids = {w["wo_id"] for w in app_db.work_orders(AS_OF)}
    po_ids = {p["po_id"] for p in app_db.purchase_orders(AS_OF)}
    approved = {r["draft_id"] for r in pending}
    check("승인한 초안이 운영 DB에 등록됨", approved <= wo_ids | po_ids, f"{sorted(approved)}")

    t0 = time.perf_counter()
    reply = services.chat(thread_id, AS_OF, QUESTION)
    print(f"채팅 ({time.perf_counter() - t0:.1f}s): {reply['answer'][:300]}")
    messages = build_supervisor().get_state(config).values["messages"]
    ids = [m.id for m in messages]
    check("채팅이 같은 대화에 이어짐 (이전 메시지 유지 + 새 질문 추가)",
          set(before["message_ids"]) <= set(ids) and len(ids) > len(before["message_ids"]))
    history = services.chat_history(thread_id, AS_OF)
    check("채팅 화면 기록에 새 질문·답변 (코드 메시지는 숨김)",
          len(history) >= 2 and history[-2] == {"role": "user", "content": QUESTION}
          and history[-1]["role"] == "assistant" and bool(history[-1]["content"])
          and not any(m["content"] == "오늘 점검을 시작해줘" for m in history), str(history)[:200])
    finish(out_path)


# ---------------------------------------------------------------- 부모: 프로세스 두 번 실행
def child(step: str, thread_id: str, state_dir: Path) -> dict:
    out_path = state_dir / f"{step}.json"
    env = {**os.environ, "FIXFIRST_STATE_DIR": str(state_dir), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, __file__, step, thread_id], env=env, cwd=ROOT_DIR)
    if proc.returncode != 0 or not out_path.exists():
        check(f"{step} 프로세스 정상 종료", False, f"exit {proc.returncode}")
        return {}
    data = json.loads(out_path.read_text(encoding="utf-8"))
    results.extend((name, ok) for name, ok in data["results"])
    return data


def main() -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="fixfirst-state-"))
    thread_id = f"persist-{uuid.uuid4().hex[:8]}"
    print(f"상태 폴더: {state_dir}\nthread_id: {thread_id}")
    if child("first", thread_id, state_dir):
        check("체크포인트·기억 파일 생성", (state_dir / "checkpoints.db").exists() and (state_dir / "memory.db").exists())
        child("second", thread_id, state_dir)

    header("결과")
    passed = sum(ok for _, ok in results)
    for name, ok in results:
        print(f"{'OK  ' if ok else 'FAIL'} {name}")
    print(f"\n{passed}/{len(results)} 통과")
    sys.exit(0 if results and passed == len(results) else 1)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        step, tid = sys.argv[1], sys.argv[2]
        state = Path(os.environ["FIXFIRST_STATE_DIR"])
        if step == "first":
            first(tid, state / "first.json")
        else:
            second(tid, state / "second.json", json.loads((state / "first.json").read_text(encoding="utf-8")))
    else:
        main()
