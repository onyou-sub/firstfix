"""리포트 에이전트 확인 (실제 OpenAI API 호출).

- 각 날짜의 evidence·plan·drafts가 없으면 먼저 run_fanout -> plan 실행
- 케이스 1: 2015-10-01, 작업 지시서 1건 APPROVED·1건 REJECTED로 바꾼 뒤 리포트 -> 5절 상태 확인 (테스트 후 원복)
- 케이스 2: 2015-12-22, 4절 "사람 확인 필요"에 13번 설비
- 케이스 3: 2015-10-06 (고위험 0대), "오늘 정비 대상 없음" 짧은 리포트가 에러 없이 저장
- 케이스 4: template_report 단독, 6개 절
"""

import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.fanout import run_fanout  # noqa: E402
import services  # noqa: E402
from agents.planner import plan  # noqa: E402
from agents.report import SECTIONS, check_report, template_report, write_report  # noqa: E402
from ml.predict import predict_df  # noqa: E402
from tools.plan_tools import DRAFTS_DIR, load_findings, load_plan  # noqa: E402
from tools.report_tools import report_path  # noqa: E402

STATUS_WORDS = {"승인": "APPROVED", "APPROVED": "APPROVED", "거절": "REJECTED", "REJECTED": "REJECTED",
                "대기": "PENDING", "PENDING": "PENDING", "대체됨": "SUPERSEDED", "SUPERSEDED": "SUPERSEDED"}
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def drafts_path(as_of: str) -> Path:
    return DRAFTS_DIR / f"{pd.Timestamp(as_of):%Y%m%d}.json"


def ensure_inputs(as_of: str) -> None:
    pred = predict_df(as_of)
    high = sorted(int(m) for m in pred.loc[pred["high_risk"], "machine_id"])
    missing = [m for m in high if m not in {f["machine_id"] for f in load_findings(as_of)}]
    if missing:
        print(f"evidence 없음 {missing} -> run_fanout")
        run_fanout(missing, as_of)
    if missing or load_plan(as_of) is None or (high and not drafts_path(as_of).exists()):
        print("plan/drafts 없음 -> plan 실행")
        plan(as_of)
    print(f"입력 준비: 고위험 {high}, 배정표 {'있음' if load_plan(as_of) else '없음'},"
          f" 초안 파일 {'있음' if drafts_path(as_of).exists() else '없음'}")


def section(markdown: str, title: str) -> str:
    m = re.search(rf"^##\s*{re.escape(title)}.*?\n(.*?)(?=^##\s|\Z)", markdown, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def statuses_in(text: str, draft_ids: list[str]) -> dict[str, str | None]:
    """각 draft_id가 나온 줄(없으면 바로 위 소제목)의 상태 단어."""
    pattern = re.compile("|".join(STATUS_WORDS))
    found, current = {}, None
    for line in text.splitlines():
        words = pattern.findall(line)
        ids = [d for d in draft_ids if d in line]
        if ids:
            for d in ids:
                found[d] = STATUS_WORDS[words[0]] if words else current
        elif words:
            current = STATUS_WORDS[words[0]]  # "### 승인" 같은 소제목
    return {d: found.get(d) for d in draft_ids}


def show(out: dict, elapsed: float) -> None:
    print(f"\n경로: {out['path'].relative_to(ROOT_DIR)}  ({elapsed:.1f}s)  fallback={out['fallback']}"
          + (f"  error={out['error']}" if out.get("error") else ""))
    for name, ok in out["checks"].items():
        print(f"  checks {name}: {'OK' if ok else 'FAIL'}")
    print("\n[리포트 앞 30줄]")
    print("\n".join(out["markdown"].splitlines()[:30]))


def run(as_of: str) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = write_report(as_of)
    return out, time.perf_counter() - t0


def case1() -> None:
    as_of = "2015-10-01 06:00:00"
    header(f"케이스 1: {as_of} (작업 지시 승인 1 / 거절 1)")
    # 다른 테스트(재계획 등)가 남긴 같은 날짜 배정 버전·초안을 지우고 v1 계획부터 다시 만든다
    services.reset_day_outputs(as_of)
    ensure_inputs(as_of)
    path = drafts_path(as_of)
    original = path.read_bytes()
    try:
        drafts = json.loads(original)
        wos = sorted(k for k, d in drafts.items() if d["type"] == "work_order")
        approved, rejected = wos[0], wos[1]
        drafts[approved]["status"], drafts[rejected]["status"] = "APPROVED", "REJECTED"
        path.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
        expected = {k: d["status"] for k, d in drafts.items()}
        print(f"테스트용 상태 변경: {approved}=APPROVED, {rejected}=REJECTED, 나머지 PENDING")

        out, elapsed = run(as_of)
        show(out, elapsed)
        got = statuses_in(section(out["markdown"], SECTIONS[4]), list(expected))
        wrong = {k: (got[k], v) for k, v in expected.items() if got[k] != v}
        check("케이스 1: 4개 checks 모두 OK", all(out["checks"].values()))
        check("케이스 1: 5절 승인 1 / 거절 1 / 나머지 대기", not wrong,
              "모두 일치" if not wrong else f"불일치(리포트, 실제): {wrong}")
        check("케이스 1: fallback 없음", not out["fallback"])
    finally:
        path.write_bytes(original)
        print(f"\n초안 상태 원복: {path.relative_to(ROOT_DIR)}")


def case2() -> None:
    as_of = "2015-12-22 06:00:00"
    header(f"케이스 2: {as_of} (13번 사람 확인)")
    ensure_inputs(as_of)
    out, elapsed = run(as_of)
    show(out, elapsed)
    sec4 = section(out["markdown"], SECTIONS[3])
    check("케이스 2: 4개 checks 모두 OK", all(out["checks"].values()))
    check("케이스 2: 4절에 13번 설비", re.search(r"#\s*13(?!\d)|(?<![\d.])13번", sec4) is not None,
          sec4.strip().splitlines()[0][:80] if sec4.strip() else "4절 비어 있음")


def case3() -> None:
    as_of = "2015-10-06 06:00:00"
    header(f"케이스 3: {as_of} (고위험 0대)")
    ensure_inputs(as_of)
    try:
        out, elapsed = run(as_of)
    except Exception as e:
        check("케이스 3: 에러 없이 저장", False, f"{type(e).__name__}: {e}")
        return
    show(out, elapsed)
    check("케이스 3: 에러 없이 저장", out["path"].exists())
    check("케이스 3: '오늘 정비 대상 없음' 포함", "오늘 정비 대상 없음" in out["markdown"])
    check("케이스 3: 6개 절", out["checks"]["④ 6개 절 제목 모두 있음"])


def case4() -> None:
    header("케이스 4: template_report 단독 (LLM 없음)")
    for as_of in ["2015-10-01 06:00:00", "2015-12-22 06:00:00", "2015-10-06 06:00:00"]:
        md = template_report(as_of)
        checks = check_report(as_of, md, report_path(as_of))
        checks.pop("① 저장 파일 존재")  # 저장하지 않고 내용만 검사
        check(f"케이스 4: {as_of[:10]} 템플릿 6개 절·설비 번호·근거 ID", all(checks.values()),
              ", ".join(f"{k} {'OK' if v else 'FAIL'}" for k, v in checks.items()))
    print("\n[template_report 2015-12-22 앞 30줄]")
    print("\n".join(template_report("2015-12-22 06:00:00").splitlines()[:30]))


def main() -> None:
    case1()
    case2()
    case3()
    case4()

    header("요약")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_report.py
