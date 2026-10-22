"""장기 기억 흐름 확인 (실제 OpenAI API 호출, 한 프로세스 안에서 진행 - 임시 저장소라 data/memory.db는 건드리지 않음).

1. 2015-10-01 고위험 설비 1대 조사 -> 장기 기억 자동 저장 확인
2. 그 설비에 엔지니어 피드백 저장
3. 2015-10-02 기준으로 같은 설비 다시 조사 -> recall_machine_memory 호출, summary에 피드백 반영 확인
"""

import json
import re
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.investigator import EVIDENCE_DIR, investigate_one  # noqa: E402
from memory.store import get_store, recall, reset_store, save_feedback  # noqa: E402
from ml.predict import predict_df  # noqa: E402

DAY1 = "2015-10-01 06:00:00"
DAY2 = "2015-10-02 06:00:00"
# 데모용 피드백: 조사 부품과 짝인 센서에 맞춘 현장 확인 결과 (핵심 표현은 summary 반영 확인에 사용)
FEEDBACK_BY_COMP = {
    "motor": ("전압 센서 단자 접촉 불량이 원인이었음", "접촉 불량"),
    "drive": ("회전수 센서(엔코더) 고정 볼트 풀림이 원인이었음", "볼트 풀림"),
    "pump": ("압력 센서 고정 볼트 풀림이 원인이었음", "볼트 풀림"),
    "bearing": ("진동 센서 고정 볼트 풀림이 원인이었음", "볼트 풀림"),
}
# 교체가 기준 시각과 같은데 "교체 (직)후 ... 이상/높음/에러/남아" 처럼 교체 후 상태를 말하는 문장 (해석 오류)
POST_CLAIM = re.compile(r"교체\S*\s*(직후|후)(에도|에|인데도)?\s*(\S+\s*)?(이상|높|에러|징후|계속|남아|지속)")

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def evidence(as_of: str, machine_id: int) -> dict:
    return json.loads((EVIDENCE_DIR / as_of[:10].replace("-", "") / f"{machine_id}.json").read_text(encoding="utf-8"))


def main() -> None:
    reset_store()  # 실행할 때마다 빈 기억에서 시작
    pred = predict_df(DAY1)
    first = pred[pred["high_risk"]].sort_values("machine_id").iloc[0]
    machine_id, comp, prob = int(first["machine_id"]), first["top_comp"], float(first["top_prob"])

    header(f"1. {DAY1} 설비 #{machine_id} {comp} 조사")
    t0 = time.perf_counter()
    finding, evidence_id = investigate_one(machine_id, comp, prob, DAY1)
    print(f"({time.perf_counter() - t0:.1f}s) {evidence_id} {finding.label}\nsummary: {finding.summary}")
    stored = get_store().get(("machine", str(machine_id)), DAY1)
    check("장기 기억 자동 저장", stored is not None and stored.value["evidence_id"] == evidence_id,
          f"namespace=('machine', '{machine_id}'), key={DAY1}")
    check("같은 시각 기준 recall에는 보이지 않음 (미래·현재 기억 차단)", recall(machine_id, DAY1) == [])

    header("2. 엔지니어 피드백 저장 (조사 부품에 맞춘 문장)")
    feedback, keyword = FEEDBACK_BY_COMP[comp]
    save_feedback(machine_id, DAY1, feedback, "eng01")
    print(f"#{machine_id} {comp} @ {DAY1}: {feedback}")
    seen = recall(machine_id, DAY2)
    check(f"{DAY2[:10]} 기준 recall에 피드백 보임", bool(seen) and seen[0]["feedback"][-1]["text"] == feedback)

    header(f"3. {DAY2} 같은 설비 #{machine_id} 다시 조사")
    prob2 = float(predict_df(DAY2).set_index("machine_id").at[machine_id, f"p_{comp}"])
    t0 = time.perf_counter()
    finding2, evidence_id2 = investigate_one(machine_id, comp, prob2, DAY2)
    print(f"({time.perf_counter() - t0:.1f}s) {evidence_id2} {finding2.label} (확률 {prob2:.4f})")
    print(f"summary: {finding2.summary}")
    print(f"checklist: {finding2.checklist}")

    calls = evidence(DAY2, machine_id)["tool_calls"]
    print("도구 호출: " + " -> ".join(c["name"] for c in calls))
    memory_calls = [c for c in calls if c["name"] == "recall_machine_memory"]
    if memory_calls:
        print(f"recall_machine_memory 출력: {memory_calls[0]['output']}")
    check("도구 호출에 recall_machine_memory 있음", bool(memory_calls))
    check("recall 출력에 피드백 포함", any(feedback in (c["output"] or "") for c in memory_calls))
    hits = [k for k in ["과거 피드백 반영", keyword] if k in finding2.summary]
    check("summary에 피드백 내용 또는 '과거 피드백 반영'", bool(hits), f"찾은 표현: {hits}")
    rule = evidence(DAY2, machine_id)["rule_check"]
    if rule["days_since"] == 0:  # 교체가 기준 시각과 같음 -> 조회 데이터는 모두 교체 전
        wrong = [m.group(0) for m in POST_CLAIM.finditer(finding2.summary)]
        check("교체 전 기록을 '교체 후'로 쓰지 않음", not wrong,
              f"교체 {rule['last_replaced']}" + (f", 발견: {wrong}" if wrong else ""))
    check(f"{DAY2[:10]} 조사도 자동 저장", len(recall(machine_id, "2015-10-03 06:00:00")) == 2)

    header("요약")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_memory_flow.py
