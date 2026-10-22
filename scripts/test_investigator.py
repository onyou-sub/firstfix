"""조사 에이전트 단독 실행 확인 (실제 OpenAI API 호출).

- 케이스 1: 2015-10-01 06:00, predict_df 고위험 설비 중 첫 번째 1대 -> label "예측 고장" 기대
- 케이스 2: 2015-12-22 06:00, 13번 설비 motor (당일 06:00 교체 직후) -> label "센서·설치 이상 가능성" 기대
- 두 케이스 모두: action_summary 있음, 모든 필드 길이 제한 준수 (schemas.LIMITS), 길이 처리 기록(length_check) 출력
"""

import json
import re
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.investigator import EVIDENCE_DIR, investigate_one, length_problems  # noqa: E402
from ml.predict import predict_df  # noqa: E402
from schemas import Finding  # noqa: E402

# 교체가 기준 시각과 같은데 "교체 (직)후 ... 이상/높음/에러/남아" 처럼 교체 후 상태를 말하는 문장 (해석 오류)
POST_CLAIM = re.compile(r"교체\S*\s*(직후|후)(에도|에|인데도)?\s*(\S+\s*)?(이상|높|에러|징후|계속|남아|지속)")


def run_case(title: str, machine_id: int, comp: str, prob: float, as_of: str, expected_label: str) -> bool:
    print(f"\n{'=' * 70}\n{title}\n  설비 {machine_id}, 예측 부품 {comp}, 확률 {prob:.4f}, 기준 {as_of}\n{'=' * 70}")
    t0 = time.perf_counter()
    finding, evidence_id = investigate_one(machine_id, comp, prob, as_of)
    elapsed = time.perf_counter() - t0

    path = EVIDENCE_DIR / as_of[:10].replace("-", "") / f"{machine_id}.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))

    print(f"[{elapsed:.1f}s] evidence_id={evidence_id}  ({path.relative_to(ROOT_DIR)})")
    print(f"도구 호출 {len(evidence['tool_calls'])}회: " + " -> ".join(c["name"] for c in evidence["tool_calls"]))
    print(f"rule_check: {evidence['rule_check']}, rule_override: {evidence['rule_override']}")
    print("\n[Finding]")
    print(json.dumps(finding.model_dump(), ensure_ascii=False, indent=2))

    ok = finding.label == expected_label
    print(f"\n[{'OK  ' if ok else 'FAIL'}] label = {finding.label!r} (기대: {expected_label!r})")

    lc = evidence.get("length_check", {})
    print(f"length_check: 처음 위반 {len(lc.get('problems', []))}건 {lc.get('problems', [])}, 다시 쓰기 {lc.get('rewritten')},"
          f" 다시 쓴 뒤 위반 {lc.get('after_rewrite', [])}, 말줄임 {lc.get('ellipsized')}")
    has_action = bool(finding.action_summary.strip())
    print(f"[{'OK  ' if has_action else 'FAIL'}] action_summary 있음: {finding.action_summary!r}")
    problems = length_problems(finding.model_dump())
    try:
        Finding.model_validate(evidence["finding"])  # 저장된 파일도 제한을 지키는지
        saved_ok = True
    except Exception as e:  # noqa: BLE001
        saved_ok, problems = False, [*problems, str(e)]
    print(f"[{'OK  ' if not problems and saved_ok else 'FAIL'}] 모든 필드 길이 제한 준수"
          + (f" ({problems})" if problems else ""))
    ok = ok and has_action and not problems and saved_ok
    if evidence["rule_check"]["days_since"] == 0:  # 교체가 기준 시각과 같음 -> 조회 데이터는 모두 교체 전
        wrong = [m.group(0) for m in POST_CLAIM.finditer(finding.summary)]
        print(f"[{'OK  ' if not wrong else 'FAIL'}] 교체 전 기록을 '교체 후'로 쓰지 않음" + (f" (발견: {wrong})" if wrong else ""))
        ok = ok and not wrong
    return ok


def main() -> None:
    results = []

    as_of = "2015-10-01 06:00:00"
    pred = predict_df(as_of)
    first = pred[pred["high_risk"]].sort_values("machine_id").iloc[0]
    results.append(run_case(
        "케이스 1: 고위험 설비 첫 번째 (예측 고장)",
        int(first["machine_id"]), first["top_comp"], float(first["top_prob"]), as_of, "예측 고장",
    ))

    as_of = "2015-12-22 06:00:00"
    pred = predict_df(as_of)
    row = pred.set_index("machine_id").loc[13]
    results.append(run_case(
        "케이스 2: 13번 설비 motor, 당일 교체 직후 (센서·설치 이상 가능성)",
        13, row["top_comp"], float(row["top_prob"]), as_of, "센서·설치 이상 가능성",
    ))

    print(f"\n결과: {sum(results)}/{len(results)} 통과")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_investigator.py
