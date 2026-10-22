"""백테스트 결과(reports/backtest.csv)를 터미널에서 표로 본다.

- 옵션 없음: 요약, K 분포, 날짜별 결과(92일), 데모 후보
- --date YYYY-MM-DD: 그날 06:00 고위험 설비별 예측 vs 실제 (predict_df, actual_failures로 다시 계산)
- --issues: 헛경보·놓침·충돌이 있는 날만
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

ML_DIR = Path(__file__).resolve().parent
ROOT_DIR = ML_DIR.parent
sys.path.insert(0, str(ML_DIR))
sys.path.insert(0, str(ROOT_DIR))

from backtest import HOUR, OUT_PATH, load_maint  # noqa: E402
from evaluation.actual import actual_failures  # noqa: E402
from predict import predict_df  # noqa: E402

DAILY_COLS = {
    "flag": "",
    "date": "날짜",
    "K": "고위험 대수",
    "hits": "적중",
    "false_alarms": "헛경보",
    "misses": "놓침",
    "conflict": "교체 직후 충돌",
}


def setup_display() -> None:
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_colwidth", 60)
    pd.set_option("display.unicode.east_asian_width", True)  # 한글 컬럼 정렬


def load_backtest() -> pd.DataFrame:
    if not OUT_PATH.exists():
        sys.exit(f"{OUT_PATH.relative_to(ROOT_DIR)}이 없습니다. 먼저 실행: uv run python ml/backtest.py")
    bt = pd.read_csv(OUT_PATH, encoding="utf-8-sig", keep_default_na=False)
    bt["issue"] = (bt["false_alarms"] > 0) | (bt["misses"] > 0) | (bt["conflict"] == 1)
    bt["flag"] = bt["issue"].map({True: "!", False: ""})
    return bt


def section(title: str) -> None:
    print(f"\n[{title}]")


def daily_table(bt: pd.DataFrame) -> pd.DataFrame:
    out = bt[list(DAILY_COLS)].rename(columns=DAILY_COLS)
    out["교체 직후 충돌"] = bt["conflict"].map({1: "충돌", 0: ""})
    return out


def show_summary(bt: pd.DataFrame) -> None:
    hits, fa, miss = int(bt["hits"].sum()), int(bt["false_alarms"].sum()), int(bt["misses"].sum())
    flagged, actual = int(bt["K"].sum()), int(bt["actual"].sum())
    section(f"요약 ({len(bt)}일, {bt['date'].iloc[0]} ~ {bt['date'].iloc[-1]}, 매일 {HOUR[:5]})")
    summary = pd.DataFrame([
        ("고위험 선별", flagged),
        ("실제 고장 설비", actual),
        ("적중", hits),
        ("헛경보", fa),
        ("놓침", miss),
        ("Recall (실제 고장 중 06시에 잡은 비율)", f"{hits / actual:.4f}" if actual else "-"),
        ("Precision (고위험 중 실제 고장 비율)", f"{hits / flagged:.4f}" if flagged else "-"),
        ("교체 직후 충돌이 있는 날", int(bt["conflict"].sum())),
    ], columns=["항목", "값"])
    print(summary.to_string(index=False))

    section("K 분포")
    k_dist = bt["K"].value_counts().sort_index().rename_axis("고위험 대수").reset_index(name="날 수")
    print(k_dist.to_string(index=False))


def show_demo(bt: pd.DataFrame) -> None:
    cols = {
        "date": "날짜", "K": "고위험 대수", "actual": "실제 고장", "hits": "적중",
        "false_alarms": "헛경보", "misses": "놓침", "high_risk_machines": "고위험 설비:부품",
    }
    section("데모 후보")

    print("\n- K=0인 날 (놓침 적은 순 3개)")
    quiet = bt[bt["K"] == 0].sort_values(["misses", "date"]).head(3)
    print(quiet[list(cols)].rename(columns=cols).to_string(index=False) if len(quiet) else "  없음")

    print("\n- K=3~5 중 적중 많은 날 (3개)")
    busy = bt[bt["K"].between(3, 5)].sort_values(["hits", "false_alarms", "date"], ascending=[False, True, True]).head(3)
    print(busy[list(cols)].rename(columns=cols).to_string(index=False) if len(busy) else "  없음")

    print("\n- 교체 직후 충돌이 있는 날 (전부, 설비:부품@교체일)")
    conflict = bt[bt["conflict"] == 1]
    conflict_cols = {"date": "날짜", "K": "고위험 대수", "hits": "적중", "conflict_machines": "충돌 설비"}
    print(conflict[list(conflict_cols)].rename(columns=conflict_cols).to_string(index=False) if len(conflict) else "  없음")


def days_since_replaced(maint: pd.DataFrame, as_of: pd.Timestamp, machine_id: int, comp: str) -> float:
    """as_of 이하 마지막 교체부터 경과일 (같은 시각 교체 포함, 기록 없으면 NaN)."""
    m = maint[(maint["machine_id"] == machine_id) & (maint["comp"] == comp) & (maint["datetime"] <= as_of)]
    return (as_of - m["datetime"].max()).total_seconds() / 86400 if len(m) else float("nan")


def show_date(date: str) -> None:
    try:
        as_of = pd.Timestamp(f"{date} {HOUR}")
    except ValueError:
        sys.exit(f"날짜 형식이 잘못됐습니다: {date} (예: 2015-10-01)")
    as_of_str = as_of.strftime("%Y-%m-%d %H:%M:%S")

    pred = predict_df(as_of_str)
    actual = actual_failures(as_of_str)
    maint = load_maint()
    actual_comps = actual.groupby("machine_id")["comp"].apply(lambda s: ", ".join(sorted(set(s))))

    flagged = pred[pred["high_risk"]].sort_values("top_prob", ascending=False)
    rows = []
    for r in flagged.itertuples():
        failed = actual_comps.get(r.machine_id, "")
        rows.append({
            "설비 번호": r.machine_id,
            "예측 부품": r.top_comp,
            "확률": f"{r.top_prob:.4f}",
            "실제 24시간 내 고장 부품": failed or "-",
            "판정": "적중" if failed else "헛경보",
            "최근 교체 경과일": days_since_replaced(maint, as_of, r.machine_id, r.top_comp),
        })

    section(f"{as_of_str} 고위험 설비 (100대 중 {len(flagged)}대)")
    if rows:
        print(pd.DataFrame(rows).to_string(index=False, float_format="{:.1f}".format))
    else:
        print("  고위험 설비 없음")

    missed_ids = sorted(set(actual_comps.index) - set(flagged["machine_id"]))
    if missed_ids:
        section("놓침 (고위험이 아니었지만 실제로 고장)")
        by_id = pred.set_index("machine_id")
        miss_rows = []
        for m in missed_ids:
            comps = actual_comps[m].split(", ")
            miss_rows.append({
                "설비 번호": m,
                "실제 고장 부품": actual_comps[m],
                "실제 부품 확률": ", ".join(f"{by_id.at[m, f'p_{c}']:.4f}" for c in comps),
                "예측 부품(최고 확률)": by_id.at[m, "top_comp"],
                "판정": "놓침",
                "최근 교체 경과일": ", ".join(f"{days_since_replaced(maint, as_of, m, c):.1f}" for c in comps),
            })
        print(pd.DataFrame(miss_rows).to_string(index=False))

    hits = sum(1 for r in rows if r["판정"] == "적중")
    print(f"\n요약: 고위험 {len(rows)}대, 적중 {hits}, 헛경보 {len(rows) - hits}, 놓침 {len(missed_ids)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="백테스트 결과(reports/backtest.csv)를 표로 출력")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="그날 06:00 기준 설비별 상세 (예: 2015-10-01)")
    group.add_argument("--issues", action="store_true", help="헛경보·놓침·충돌이 있는 날만 출력")
    args = parser.parse_args()

    setup_display()
    if args.date:
        show_date(args.date)
        return

    bt = load_backtest()
    if args.issues:
        issues = bt[bt["issue"]]
        section(f"문제 있는 날 ({len(issues)}일 / {len(bt)}일)")
        print(daily_table(issues).to_string(index=False) if len(issues) else "  없음")
        return

    show_summary(bt)
    section("날짜별 결과 (! = 헛경보·놓침·충돌 있음)")
    print(daily_table(bt).to_string(index=False))
    show_demo(bt)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, ml/backtest.py 실행 후):
#   uv run python ml/show_backtest.py                     # 요약 + K 분포 + 날짜별 결과 + 데모 후보
#   uv run python ml/show_backtest.py --date 2015-10-01   # 그날 06:00 고위험 설비 상세
#   uv run python ml/show_backtest.py --issues            # 헛경보·놓침·충돌이 있는 날만
