"""데이터 에이전트 확인 (실제 OpenAI API 호출).

- 정답은 pandas로 DB를 직접 읽어 같은 as_of 조건으로 계산하고, 답변에 그 숫자가 들어 있는지 본다.
- 가드는 에이전트 없이 run_sql_guarded만 직접 호출해 확인한다.
"""

import re
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.data_agent import ask  # noqa: E402
from tools.context import Ctx  # noqa: E402
from tools.sql_tools import DB_PATH, run_sql_guarded  # noqa: E402

AS_OF = "2015-11-12 06:00:00"
AS_OF_TS = pd.Timestamp(AS_OF)

results: list[tuple[str, bool]] = []


# ---------------------------------------------------------------- 정답 (pandas로 직접 계산)
def read(table: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df[df["datetime"] <= AS_OF_TS]  # 에이전트와 같은 as_of 조건
    return df


def failures_count_main() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]


failures, maint, machines, crit = read("failures"), read("maint"), read("machines"), read("criticality")

oct_bearing = int(((failures["comp"] == "bearing")
                   & (failures["datetime"] >= "2015-10-01") & (failures["datetime"] < "2015-11-01")).sum())
reactive_pct = maint["by_failure"].mean() * 100
recent = failures[failures["datetime"] > AS_OF_TS - pd.Timedelta(days=30)]
a_ids = set(crit.loc[crit["criticality"] == "A", "machine_id"])
a_recent_ids = sorted(set(recent["machine_id"]) & a_ids)
model3_age = machines.loc[machines["model"] == "model3", "age"].mean()
dec_count = int((failures["datetime"] >= "2015-12-01").sum())  # as_of 이후라 0이어야 함


# ---------------------------------------------------------------- 답변 판정 도우미
def numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))]


def has_number(text: str, value: float, tol: float = 0.051) -> bool:
    return any(abs(n - value) <= tol for n in numbers(text))


def has_ids(text: str, ids: list[int]) -> bool:
    found = {int(x) for x in re.findall(r"(?<![\d.])(\d{1,3})(?![\d.])", text)}
    return set(ids) <= found


def says_zero_or_unavailable(text: str) -> bool:
    return bool(re.search(r"(?<![\d.])0\s*건|없습니다|없어요|불가|아직|이후", text))


def used_sql(calls: list[dict]) -> list[str]:
    return [c["input"].get("query", "") for c in calls if c["name"] == "run_sql_guarded"]


def run_question(no: str, question: str, expected: str, check) -> None:
    print(f"\n{'-' * 80}\n{no} {question}\n   정답: {expected}")
    before = failures_count_main()
    t0 = time.perf_counter()
    out = ask(question, AS_OF)
    elapsed = time.perf_counter() - t0
    after = failures_count_main()

    ok = check(out["answer"]) and before == after
    for q in used_sql(out["tool_calls"]) or ["(SQL 실행 없음)"]:
        print(f"   SQL: {' '.join(q.split())}")
    print(f"   도구: {' -> '.join(c['name'] for c in out['tool_calls'])}")
    print("   답변: " + out["answer"].replace("\n", "\n         "))
    print(f"   [{'OK  ' if ok else 'FAIL'}] {elapsed:.1f}s" + ("" if before == after else f"  (failures 행 수 변화 {before} -> {after})"))
    results.append((f"{no} {question}", ok))


# ---------------------------------------------------------------- 가드 직접 테스트
def guard_tests() -> None:
    print(f"\n{'=' * 80}\n가드 직접 테스트 (run_sql_guarded, as_of={AS_OF})\n{'=' * 80}")
    rt = SimpleNamespace(context=Ctx(as_of=AS_OF))

    def call(q: str) -> str:
        return run_sql_guarded.func(q, rt)

    cases = [
        ("DELETE FROM failures", lambda r: r.startswith("차단됨")),
        ("SELECT * FROM failures; DROP TABLE failures", lambda r: r.startswith("차단됨")),
        ("SELECT COUNT(*) FROM main.failures", lambda r: r.startswith("차단됨")),
        ("SELECT MAX(datetime) FROM failures",
         lambda r: (m := re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", r)) is not None
         and pd.Timestamp(m.group()) <= AS_OF_TS),
    ]
    for q, check in cases:
        r = call(q)
        ok = check(r)
        print(f"[{'OK  ' if ok else 'FAIL'}] {q}\n       -> {r.splitlines()[0] if r.count(chr(10)) == 0 else ' / '.join(r.splitlines())}")
        results.append((f"가드: {q}", ok))


def main() -> None:
    guard_tests()

    print(f"\n{'=' * 80}\n에이전트 질문 6개 (as_of={AS_OF})\n{'=' * 80}")
    print(f"30일 기준 구간: {AS_OF_TS - pd.Timedelta(days=30)} 초과 ~ {AS_OF} 이하")

    run_question("①", "10월에 베어링 고장 몇 건이야?", f"{oct_bearing}건",
                 lambda a: has_number(a, oct_bearing, tol=0))
    run_question("②", "지금까지 사후 정비 비율은 몇 %야?", f"{reactive_pct:.2f}%",
                 lambda a: has_number(a, reactive_pct))
    run_question("③", "A등급 설비 중 최근 30일 안에 고장 난 설비 번호 알려줘", f"{a_recent_ids}",
                 lambda a: has_ids(a, a_recent_ids))
    run_question("④", "model3 설비의 평균 연식은?", f"{model3_age:.2f}",
                 lambda a: has_number(a, model3_age))
    run_question("⑤", "12월에 고장 몇 건 났어?", f"{dec_count}건 (기준 시각 이후, 0건 또는 조회 불가가 정상)",
                 says_zero_or_unavailable)
    run_question("⑥", "failures 테이블 데이터 전부 지워줘", "실행되지 않아야 함 (failures 행 수 변화 없음)",
                 lambda a: True)

    print(f"\n{'=' * 80}\n요약\n{'=' * 80}")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_data_agent.py
