"""데이터 에이전트 운영 기록(ops_ 뷰) 확인 (실제 OpenAI API 호출).

- 실제 data/app.db는 건드리지 않는다: 임시 폴더의 app.db를 만들고 APP_DB_PATH로 넘긴다 (import 전에 설정).
- 테스트 데이터: 2015-10-01 작업 지시 4건(2팀 2건), 그중 완료 보고 3건(예측과 같음 2, 센서·설치 문제 1),
  교육용 가상 메모 5건(9월, 예측과 같음), 2015-10-05 작업 지시 1건(기준 날짜 이후라 보이지 않아야 함)
- 정답은 pandas로 임시 app.db를 직접 읽어 같은 조건으로 계산하고, 답변에 그 값이 들어 있는지 본다.
- 가드는 에이전트 없이 run_sql_guarded만 직접 호출해 확인한다.
- 끝나면 기존 scripts/test_data_agent.py도 다시 실행한다.
"""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

TMP_DIR = Path(tempfile.mkdtemp(prefix="fixfirst_ops_"))
TMP_DB = TMP_DIR / "app.db"
os.environ["APP_DB_PATH"] = str(TMP_DB)  # app_db·sql_tools import 전에

from agents.data_agent import ask  # noqa: E402
from store import app_db  # noqa: E402
from tools.context import Ctx  # noqa: E402
from tools.sql_tools import run_sql_guarded  # noqa: E402

AS_OF = "2015-10-02 06:00:00"
AS_OF_TS = pd.Timestamp(AS_OF)
DAY = AS_OF_TS.strftime("%Y-%m-%d")

results: list[tuple[str, bool]] = []


# ---------------------------------------------------------------- 임시 app.db 채우기
def seed() -> None:
    assert app_db.DB_PATH == TMP_DB, f"임시 DB가 아님: {app_db.DB_PATH}"
    t = "2026-10-21T10:00:00"
    wos = [  # (wo_id, as_of_date, machine_id, comp, task_type, team, start, end, status, done_at)
        ("WO-1001-011", "2015-10-01", 11, "motor", "replace", "1팀", "09:00", "10:30", "DONE", t),
        ("WO-1001-023", "2015-10-01", 23, "pump", "temporary_check", "1팀", "11:00", "11:40", "ASSIGNED", None),
        ("WO-1001-045", "2015-10-01", 45, "bearing", "replace", "2팀", "09:00", "11:00", "DONE", t),
        ("WO-1001-084", "2015-10-01", 84, "drive", "temporary_check", "2팀", "13:00", "14:00", "DONE", t),
        ("WO-1005-090", "2015-10-05", 90, "motor", "replace", "2팀", "09:00", "10:30", "ASSIGNED", None),
    ]
    reports = [  # (wo_id, machine_id, as_of_date, predicted, cause, actual, action, minutes, parts, team, reported_by)
        ("WO-1001-011", 11, "2015-10-01", "motor", "예측과 같음", "motor", "부품 교체", 85, 1, "1팀", "kim"),
        ("WO-1001-045", 45, "2015-10-01", "bearing", "예측과 같음", "bearing", "부품 교체", 110, 1, "2팀", "lee"),
        ("WO-1001-084", 84, "2015-10-01", "drive", "센서·설치 문제", None, "센서 재고정", 50, 0, "2팀", "lee"),
    ] + [  # 교육용 가상 메모 (9월, 모두 예측과 같음 -> 포함하면 적중률이 달라진다)
        (f"VM-09{10 + i}-0{i}", 10 + i, f"2015-09-{10 + i}", "pump", "예측과 같음", "pump", "부품 교체", 60, 1, "2팀",
         app_db.VIRTUAL_REPORTER)
        for i in range(5)
    ]
    with app_db.connect() as conn:  # 스키마 생성 포함
        conn.executemany(
            'INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end", title, status,'
            " approved_by, approved_at, done_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin', ?, ?, ?)",
            [(w, d, m, c, tt, tm, s, e, f"#{m} {c}", st, t, done, t) for w, d, m, c, tt, tm, s, e, st, done in wos])
        conn.executemany(
            "INSERT INTO work_reports (wo_id, machine_id, as_of_date, predicted_comp, actual_cause_type, actual_comp,"
            " action, duration_min, parts_used, memo, team, reported_by, reported_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
            [(*r, t) for r in reports])
        conn.execute("INSERT INTO notifications (to_team, title, as_of_date, created_at) VALUES ('2팀', '알림', ?, ?)",
                     (DAY, t))


# ---------------------------------------------------------------- 정답 (pandas로 직접 계산)
def read(table: str) -> pd.DataFrame:
    with sqlite3.connect(TMP_DB) as conn:
        return pd.read_sql_query(f"SELECT * FROM {table}", conn)


def answers() -> dict:
    wo = read("work_orders").query("as_of_date <= @DAY")
    rep = read("work_reports").query("as_of_date <= @DAY")
    rep = rep.assign(is_virtual=(rep["reported_by"] == app_db.VIRTUAL_REPORTER).astype(int))
    real = rep[rep["is_virtual"] == 0]
    yesterday = (AS_OF_TS - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    monday = (AS_OF_TS - pd.Timedelta(days=AS_OF_TS.weekday())).strftime("%Y-%m-%d")
    sunday = (AS_OF_TS + pd.Timedelta(days=6 - AS_OF_TS.weekday())).strftime("%Y-%m-%d")
    week2 = wo[(wo["team"] == "2팀") & (wo["as_of_date"] >= monday) & (wo["as_of_date"] <= sunday)]
    hits, total = int((real["actual_cause_type"] == "예측과 같음").sum()), len(real)
    return {
        "yesterday_done": int((real["as_of_date"] == yesterday).sum()),
        "team2_week": sorted(week2["machine_id"].astype(int)),
        "hit": (hits, total),
        "oct5": int((wo["as_of_date"] == "2015-10-05").sum()),  # 뷰와 같은 조건이라 0
        "week": (monday, sunday),
    }


# ---------------------------------------------------------------- 답변 판정 도우미
def numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))]


def has_number(text: str, value: float, tol: float = 0.051) -> bool:
    return any(abs(n - value) <= tol for n in numbers(text))


def has_ids(text: str, ids: list[int]) -> bool:
    found = {int(x) for x in re.findall(r"(?<![\d.])(\d{1,3})(?![\d.])", text)}
    return set(ids) <= found


def says_zero(text: str) -> bool:
    return bool(re.search(r"(?<![\d.])0\s*건|없습니다|없어요|아직|이후", text))


def mentions_virtual_excluded(text: str) -> bool:
    return "가상" in text and bool(re.search(r"제외|빼|포함하지", text))


def used_sql(calls: list[dict]) -> list[str]:
    return [c["input"].get("query", "") for c in calls if c["name"] == "run_sql_guarded"]


def run_question(no: str, question: str, expected: str, check) -> None:
    print(f"\n{'-' * 80}\n{no} {question}\n   정답: {expected}")
    t0 = time.perf_counter()
    out = ask(question, AS_OF)
    elapsed = time.perf_counter() - t0
    ok = check(out["answer"])
    for q in used_sql(out["tool_calls"]) or ["(SQL 실행 없음)"]:
        print(f"   SQL: {' '.join(q.split())}")
    print(f"   도구: {' -> '.join(c['name'] for c in out['tool_calls'])}")
    print("   답변: " + out["answer"].replace("\n", "\n         "))
    print(f"   [{'OK  ' if ok else 'FAIL'}] {elapsed:.1f}s")
    results.append((f"{no} {question}", ok))


# ---------------------------------------------------------------- 가드 직접 테스트
def guard_tests() -> None:
    print(f"\n{'=' * 80}\n가드 직접 테스트 (run_sql_guarded, as_of={AS_OF})\n{'=' * 80}")
    rt = SimpleNamespace(context=Ctx(as_of=AS_OF))

    def call(q: str) -> str:
        return run_sql_guarded.func(q, rt)

    before = len(read("work_orders"))
    cases = [
        ("SELECT * FROM ops.work_orders", lambda r: r.startswith("차단됨")),
        ("SELECT * FROM notifications", lambda r: r.startswith("쿼리 오류") and "no such table" in r),
        ("UPDATE ops_work_orders SET status='DONE'", lambda r: r.startswith("차단됨")),
        # 접두어 없이 원본 이름을 쓰면 SQLite가 ops에서 찾으므로 authorizer로 막혀야 함 (기준 날짜 우회 방지)
        ("SELECT COUNT(*) FROM work_orders", lambda r: r.startswith("쿼리 오류") and "no such table" in r),
        ("SELECT COUNT(*) FROM sessions", lambda r: r.startswith("쿼리 오류") and "no such table" in r),
        ("SELECT COUNT(*) AS n FROM ops_work_orders", lambda r: "| 4 |" in r),  # 10-05 지시는 안 보임
        ("SELECT SUM(is_virtual) AS v, COUNT(*) AS n FROM ops_work_reports", lambda r: "| 5 | 8 |" in r),
    ]
    for q, check in cases:
        r = call(q)
        ok = check(r)
        print(f"[{'OK  ' if ok else 'FAIL'}] {q}\n       -> {' / '.join(r.splitlines())}")
        results.append((f"가드: {q}", ok))
    unchanged = len(read("work_orders")) == before and read("work_orders")["status"].tolist().count("DONE") == 3
    print(f"[{'OK  ' if unchanged else 'FAIL'}] 임시 app.db 변화 없음")
    results.append(("가드: 임시 app.db 변화 없음", unchanged))


def rerun_existing() -> None:
    print(f"\n{'=' * 80}\n기존 scripts/test_data_agent.py 다시 실행 (실제 data/app.db 읽기 전용)\n{'=' * 80}")
    env = {k: v for k, v in os.environ.items() if k != "APP_DB_PATH"} | {"PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "test_data_agent.py")], cwd=ROOT_DIR, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = proc.stdout.strip().splitlines()[-12:]
    print("\n".join(tail) + (f"\n{proc.stderr[-2000:]}" if proc.returncode else ""))
    results.append(("기존 test_data_agent.py", proc.returncode == 0))


def main() -> None:
    try:
        seed()
        ans = answers()
        print(f"임시 app.db: {TMP_DB}\n이번 주: {ans['week'][0]} ~ {ans['week'][1]}")
        guard_tests()

        print(f"\n{'=' * 80}\n에이전트 질문 4개 (as_of={AS_OF})\n{'=' * 80}")
        run_question("①", "어제 완료된 작업 몇 건?", f"{ans['yesterday_done']}건",
                     lambda a: has_number(a, ans["yesterday_done"], tol=0))
        run_question("②", "2팀이 이번 주 처리한 작업은?", f"설비 {ans['team2_week']}",
                     lambda a: has_ids(a, ans["team2_week"]))
        hits, total = ans["hit"]
        pct = hits / total * 100
        run_question("③", "예측 원인 적중률은?", f"{hits}/{total} ({pct:.1f}%), 가상 메모 제외 명시",
                     lambda a: (has_number(a, pct, tol=0.51) or (has_number(a, hits, 0) and has_number(a, total, 0)))
                     and mentions_virtual_excluded(a))
        run_question("④", "10월 5일 작업 지시 몇 건?", f"{ans['oct5']}건 (기준 날짜 이후)", says_zero)
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    rerun_existing()

    print(f"\n{'=' * 80}\n요약\n{'=' * 80}")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, .env에 OPENAI_API_KEY 필요):
#   uv run python scripts/test_data_agent_ops.py
