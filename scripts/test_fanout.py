"""병렬 조사(fan-out) 확인 (실제 OpenAI API 호출).

- 케이스 1: 2015-10-01 06:00 고위험 5대 -> 순차(for문) vs 병렬(run_fanout) 시간 비교
- 케이스 2: 2015-10-06 06:00 고위험 0대 -> 조사 없이 즉시 반환
- 케이스 3: 2015-12-22 06:00 13번 설비 -> "센서·설치 이상 가능성"
- investigate_machines 도구가 돌려주는 문자열 확인 (Supervisor가 받게 될 형태)
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

import agents.fanout as fanout  # noqa: E402
from agents.investigator import EVIDENCE_DIR, investigate_one  # noqa: E402
from ml.predict import predict_df  # noqa: E402
from tools.context import Ctx  # noqa: E402
from tools.investigate_tool import format_results, investigate_machines  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def high_risk_ids(as_of: str) -> list[int]:
    pred = predict_df(as_of)
    return sorted(int(m) for m in pred.loc[pred["high_risk"], "machine_id"])


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def case1() -> None:
    as_of = "2015-10-01 06:00:00"
    ids = high_risk_ids(as_of)
    header(f"케이스 1: {as_of} 고위험 {len(ids)}대 {ids}")
    targets = fanout.make_targets(ids, as_of)

    print("순차 실행 (for문으로 investigate_one 반복)...")
    t0 = time.perf_counter()
    for t in targets:
        investigate_one(t["machine_id"], t["comp"], t["prob"], as_of)
    sequential = time.perf_counter() - t0
    print(f"  순차: {sequential:.1f}s")

    print(f"병렬 실행 (run_fanout, max_concurrency={fanout.MAX_CONCURRENCY})...")
    started = time.time()
    t0 = time.perf_counter()
    out = fanout.run_fanout(ids, as_of)
    parallel = time.perf_counter() - t0
    print(f"  병렬: {parallel:.1f}s  (순차 대비 {sequential / parallel:.1f}배)\n")
    print(format_results(out))
    print()

    check("케이스 1: 결과 5개", len(out) == 5, f"{len(out)}개")
    check("케이스 1: 모두 status ok", all(r["status"] == "ok" for r in out),
          ", ".join(f"#{r['machine_id']}={r['status']}" for r in out))
    folder = EVIDENCE_DIR / as_of[:10].replace("-", "")
    fresh = [m for m in ids if (p := folder / f"{m}.json").exists() and p.stat().st_mtime >= started]
    check("케이스 1: evidence 파일 5개 (병렬 실행에서 새로 저장)", len(fresh) == 5,
          f"{len(fresh)}개, {folder.relative_to(ROOT_DIR)}")
    check("병렬 시간 < 순차 시간", parallel < sequential, f"병렬 {parallel:.1f}s / 순차 {sequential:.1f}s")


def case2() -> None:
    as_of = "2015-10-06 06:00:00"
    ids = high_risk_ids(as_of)
    header(f"케이스 2: {as_of} 고위험 {len(ids)}대")

    calls = []
    original = fanout.investigate_one
    fanout.investigate_one = lambda *a, **k: calls.append(a) or original(*a, **k)  # 조사 에이전트 실행 수 세기
    try:
        t0 = time.perf_counter()
        out = fanout.run_fanout(ids, as_of)
        elapsed = time.perf_counter() - t0
    finally:
        fanout.investigate_one = original

    check("케이스 2: 결과 0개", len(out) == 0, f"{len(out)}개, 조사 에이전트 실행 {len(calls)}회")
    check("케이스 2: 1초 이내 반환", elapsed < 1.0, f"{elapsed:.3f}s")
    print("\n[investigate_machines 출력]")
    print(investigate_machines.func(ids, SimpleNamespace(context=Ctx(as_of=as_of))))


def case3() -> None:
    as_of = "2015-12-22 06:00:00"
    header(f"케이스 3: {as_of} 13번 설비")
    out = fanout.run_fanout([13], as_of)
    r = out[0] if out else {}
    check("케이스 3: 13번 label = 센서·설치 이상 가능성", r.get("label") == "센서·설치 이상 가능성",
          f"label={r.get('label')}, status={r.get('status')}")


def tool_output() -> None:
    as_of = "2015-10-01 06:00:00"
    ids = high_risk_ids(as_of)
    header(f"investigate_machines 도구 출력 ({as_of}, 입력 {ids})")
    rt = SimpleNamespace(context=Ctx(as_of=as_of))
    t0 = time.perf_counter()
    print(investigate_machines.func(ids, rt))
    print(f"({time.perf_counter() - t0:.1f}s)")

    print("\n[입력 검증]")
    for bad in ([0, 5], list(range(1, 13)), [5, 5, 101]):
        print(f"  {bad!r:<45} -> {investigate_machines.func(bad, rt)}")


def main() -> None:
    case1()
    case2()
    case3()
    tool_output()

    header("요약")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_fanout.py
