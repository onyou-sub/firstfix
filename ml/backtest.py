"""테스트 구간(2015-10-01 ~ 2015-12-31) 매일 06:00에 predict_df를 돌려 실제 고장과 맞춰 본다. LLM은 쓰지 않는다.

- 날짜별: 고위험 대수(K), 적중(고위험 & 24시간 내 실제 고장), 헛경보, 놓침 (설비 단위)
- 교체 직후 충돌: 고위험 설비의 top_comp를 최근 7일 안에 교체한 기록이 maint에 있으면 1
- 결과: reports/backtest.csv, 요약과 데모 날짜 후보 출력
"""

import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

ML_DIR = Path(__file__).resolve().parent
ROOT_DIR = ML_DIR.parent
sys.path.insert(0, str(ML_DIR))
sys.path.insert(0, str(ROOT_DIR))

from evaluation.actual import actual_failures  # noqa: E402
from features import DB_PATH, DATETIME_FORMAT  # noqa: E402
from predict import predict_df  # noqa: E402

OUT_PATH = ROOT_DIR / "reports" / "backtest.csv"
START, END = "2015-10-01", "2015-12-31"
HOUR = "06:00:00"
RECENT_REPLACE = pd.Timedelta(days=7)


def load_maint() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        maint = pd.read_sql_query("SELECT datetime, machine_id, comp, by_failure FROM maint", conn)
    maint["datetime"] = pd.to_datetime(maint["datetime"], format=DATETIME_FORMAT)
    return maint


def recent_replacements(maint: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """(as_of - 7일, as_of] 교체 기록. as_of 이하만 보므로 미래 데이터가 아니다."""
    m = maint[(maint["datetime"] > as_of - RECENT_REPLACE) & (maint["datetime"] <= as_of)]
    return m.groupby(["machine_id", "comp"], as_index=False)["datetime"].max()


def ids(values) -> str:
    return " ".join(str(v) for v in sorted(values))


def run_day(as_of: pd.Timestamp, maint: pd.DataFrame) -> dict:
    as_of_str = as_of.strftime(DATETIME_FORMAT)
    pred = predict_df(as_of_str)
    actual = actual_failures(as_of_str)

    flagged = pred[pred["high_risk"]]
    flagged_ids = set(flagged["machine_id"])
    failed_ids = set(actual["machine_id"])
    hit_ids = flagged_ids & failed_ids
    actual_pairs = set(zip(actual["machine_id"], actual["comp"]))
    comp_hits = sum((m, c) in actual_pairs for m, c in zip(flagged["machine_id"], flagged["top_comp"]))

    recent = recent_replacements(maint, as_of)
    conflict = flagged.merge(recent, left_on=["machine_id", "top_comp"], right_on=["machine_id", "comp"])

    return {
        "date": as_of.strftime("%Y-%m-%d"),
        "as_of": as_of_str,
        "K": len(flagged_ids),
        "actual": len(failed_ids),
        "hits": len(hit_ids),
        "hits_comp": comp_hits,  # 부품까지 맞힌 적중
        "false_alarms": len(flagged_ids - failed_ids),
        "misses": len(failed_ids - flagged_ids),
        "conflict": int(len(conflict) > 0),
        "n_conflict": len(conflict),
        "high_risk_machines": " ".join(f"{m}:{c}" for m, c in sorted(zip(flagged["machine_id"], flagged["top_comp"]))),
        "actual_failures": " ".join(f"{m}:{c}" for m, c in sorted(actual_pairs)),
        "conflict_machines": " ".join(
            f"{m}:{c}@{d:%m-%d}" for m, c, d in sorted(zip(conflict["machine_id"], conflict["top_comp"], conflict["datetime"]))
        ),
        "missed_machines": ids(failed_ids - flagged_ids),
    }


def summarize(bt: pd.DataFrame) -> None:
    hits, fa, miss = bt["hits"].sum(), bt["false_alarms"].sum(), bt["misses"].sum()
    flagged, actual = bt["K"].sum(), bt["actual"].sum()
    print(f"\n== 요약: {len(bt)}일 ({bt['date'].iloc[0]} ~ {bt['date'].iloc[-1]}, 매일 {HOUR}) ==")
    print(f"  고위험 선별 합계   {flagged:>5}  (하루 평균 {flagged / len(bt):.2f}대)")
    print(f"  실제 고장 설비 합계 {actual:>5}")
    print(f"  적중              {hits:>5}  (부품까지 맞힘 {bt['hits_comp'].sum()})")
    print(f"  헛경보            {fa:>5}")
    print(f"  놓침              {miss:>5}")
    print(f"  Recall    {hits / actual if actual else float('nan'):.4f}  (실제 고장 중 06시에 잡은 비율)")
    print(f"  Precision {hits / flagged if flagged else float('nan'):.4f}  (고위험 중 실제 고장 비율)")
    print(f"  교체 직후 충돌이 있는 날 {bt['conflict'].sum()}일, 충돌 설비 합계 {bt['n_conflict'].sum()}")

    print("\n[K 분포] (하루 고위험 대수 -> 날 수)")
    for k, n in bt["K"].value_counts().sort_index().items():
        print(f"  {k}대: {n:>3}일  {'#' * n}")


def demo_candidates(bt: pd.DataFrame) -> None:
    cols = ["date", "K", "actual", "hits", "false_alarms", "misses", "high_risk_machines"]
    print("\n== 데모 날짜 후보 ==")

    # 조용한 날: 놓친 고장도 없는 날을 우선
    quiet = bt[bt["K"] == 0].sort_values(["misses", "date"]).head(3)
    print("\n[K=0인 날 3개] (놓침 적은 순)")
    print(quiet[cols].to_string(index=False) if len(quiet) else "  없음")

    busy = bt[bt["K"].between(3, 5)].sort_values(["hits", "false_alarms", "date"], ascending=[False, True, True]).head(3)
    print("\n[K=3~5 중 적중 많은 날 3개]")
    print(busy[cols].to_string(index=False) if len(busy) else "  없음")

    conflict = bt[bt["conflict"] == 1]
    print(f"\n[교체 직후 충돌 설비가 있는 날 전부] ({len(conflict)}일, 형식 설비:부품@교체일)")
    print(conflict[["date", "K", "hits", "conflict_machines"]].to_string(index=False) if len(conflict) else "  없음")


def main() -> None:
    pd.set_option("display.width", 200)
    t0 = time.perf_counter()
    maint = load_maint()
    days = pd.date_range(f"{START} {HOUR}", f"{END} {HOUR}", freq="D")

    rows = []
    for i, as_of in enumerate(days, 1):
        rows.append(run_day(as_of, maint))
        if i % 10 == 0 or i == len(days):
            print(f"  [{time.perf_counter() - t0:6.1f}s] {i}/{len(days)}일 완료 ({as_of:%Y-%m-%d})", flush=True)

    bt = pd.DataFrame(rows)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    bt.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"저장: {OUT_PATH.relative_to(ROOT_DIR)}")

    summarize(bt)
    demo_candidates(bt)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, ml/train.py 실행 후):
#   uv run python ml/backtest.py
