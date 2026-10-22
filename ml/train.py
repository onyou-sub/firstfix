"""data/features.parquet로 고장 부품 예측 모델(LightGBM 다중 분류)을 학습·평가·저장한다.

- 시간 기준 분할: 학습 2015-01~08 / 검증 2015-09 / 테스트 2015-10~12
  (라벨이 24시간 뒤까지 보므로 학습·검증 구간의 마지막 24시간은 버린다)
- 검증 구간: early stopping + 부품별 임계값 결정 (Recall >= 0.80 중 가장 큰 임계값)
- 테스트 구간: 최종 평가만 한 번 실행
- 결과: models/lgbm.pkl, reports/model_eval.md
"""

import pickle
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, confusion_matrix

ML_DIR = Path(__file__).resolve().parent
ROOT_DIR = ML_DIR.parent
sys.path.insert(0, str(ML_DIR))

from features import COMPS, FEATURE_COLS, FEATURES_PATH  # noqa: E402

MODEL_PATH = ROOT_DIR / "models" / "lgbm.pkl"
REPORT_PATH = ROOT_DIR / "reports" / "model_eval.md"

CLASSES = ["none", "motor", "drive", "pump", "bearing"]  # 모델 출력 열 순서 (고정)
MODEL_CATEGORIES = ["model1", "model2", "model3", "model4"]
LABEL_HORIZON = pd.Timedelta(hours=24)
THRESHOLD_RULE = "precision>=0.90, max recall, middle of range"
THRESHOLD_CANDIDATES = np.round(np.arange(5, 96) / 100, 2)  # 0.05 ~ 0.95, 0.01 간격
MIN_PRECISION = 0.90
MORNING_HOUR = 6
RANDOM_STATE = 42

# 기간은 [start, end) 반열린 구간. purge=True면 라벨이 다음 구간을 보지 않도록 끝 24시간을 버린다.
PERIODS = {
    "train": {"start": "2015-01-01", "end": "2015-09-01", "purge": True},
    "valid": {"start": "2015-09-01", "end": "2015-10-01", "purge": True},
    "test": {"start": "2015-10-01", "end": "2016-01-01", "purge": False},
}

assert CLASSES[1:] == COMPS, "CLASSES 순서가 features.COMPS와 달라졌습니다"

_t0 = time.perf_counter()


def log(msg: str) -> None:
    print(f"[{time.perf_counter() - _t0:7.1f}s] {msg}", flush=True)


# ---------------------------------------------------------------- 1. 데이터 분할
def load_table() -> pd.DataFrame:
    df = pd.read_parquet(FEATURES_PATH)
    df["model"] = pd.Categorical(df["model"].astype(str), categories=MODEL_CATEGORIES)
    unknown = sorted(set(df["label"]) - set(CLASSES))
    assert not unknown, f"알 수 없는 라벨: {unknown}"
    return df


def period_bounds(name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """구간 name에 실제로 쓰는 행의 [start, end) 시각."""
    p = PERIODS[name]
    start, end = pd.Timestamp(p["start"]), pd.Timestamp(p["end"])
    if p["purge"]:
        end = end - LABEL_HORIZON  # 라벨 창 (t, t+24h]이 다음 구간에 걸치는 행 제외
    return start, end


def split(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    parts = {}
    for name in PERIODS:
        start, end = period_bounds(name)
        parts[name] = df[(df["datetime"] >= start) & (df["datetime"] < end)].reset_index(drop=True)
    return parts


def print_split(parts: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for name, part in parts.items():
        counts = part["label"].value_counts().reindex(CLASSES, fill_value=0)
        rows.append({
            "구간": name,
            "시작": part["datetime"].min().strftime("%Y-%m-%d %H:%M"),
            "끝": part["datetime"].max().strftime("%Y-%m-%d %H:%M"),
            "행 수": len(part),
            **{c: int(counts[c]) for c in CLASSES},
            "고장 비율": 1 - counts["none"] / len(part),
        })
    table = pd.DataFrame(rows)
    print(table.to_string(index=False, formatters={"행 수": "{:,}".format, "고장 비율": "{:.3%}".format}))
    return table


# ---------------------------------------------------------------- 2. 학습
def encode_labels(labels: pd.Series) -> np.ndarray:
    return labels.map({c: i for i, c in enumerate(CLASSES)}).to_numpy()


def train_model(train: pd.DataFrame, valid: pd.DataFrame) -> lgb.LGBMClassifier:
    model = lgb.LGBMClassifier(
        objective="multiclass",
        class_weight="balanced",
        n_estimators=2000,  # 상한. 실제 트리 수는 early stopping이 정한다
        random_state=RANDOM_STATE,
        verbose=-1,
    )
    model.fit(
        train[FEATURE_COLS], encode_labels(train["label"]),
        eval_X=(valid[FEATURE_COLS],),
        eval_y=(encode_labels(valid["label"]),),
        eval_names=["valid"],
        eval_metric="multi_logloss",
        categorical_feature=["model"],
        callbacks=[lgb.early_stopping(50, verbose=True), lgb.log_evaluation(50)],
    )
    # 정수 라벨 0..4로 학습했으므로 predict_proba 열 순서 = CLASSES
    assert list(model.classes_) == list(range(len(CLASSES)))
    return model


def predict_proba(model: lgb.LGBMClassifier, df: pd.DataFrame) -> pd.DataFrame:
    proba = model.predict_proba(df[FEATURE_COLS], num_iteration=model.best_iteration_)
    return pd.DataFrame(proba, columns=CLASSES, index=df.index)


# ---------------------------------------------------------------- 3. 부품별 임계값
def recall_precision(proba: np.ndarray, is_pos: np.ndarray, t: float) -> tuple[float, float]:
    """부품 확률 단독 기준 (proba >= t 를 그 부품 고장으로 볼 때)."""
    hit = proba >= t
    tp = int((hit & is_pos).sum())
    recall = tp / is_pos.sum() if is_pos.sum() else float("nan")
    precision = tp / hit.sum() if hit.sum() else float("nan")
    return recall, precision


def pick_threshold(proba: np.ndarray, is_pos: np.ndarray) -> dict:
    """후보 중 Precision >= MIN_PRECISION 인 것만 남기고, Recall이 가장 높은 후보 범위의 가운데 값.

    Recall은 t에 대해 단조 감소하므로 최고 Recall 후보들은 연속 구간이고,
    그 구간 안에서는 TP가 같고 FP만 줄어 가운데 값도 Precision 조건을 만족한다.
    조건을 만족하는 후보가 없으면 threshold=None (fallback: argmax 규칙).
    """
    if not is_pos.any():
        raise ValueError("검증 구간에 양성이 없어 임계값을 정할 수 없습니다")
    scores = [(t, *recall_precision(proba, is_pos, t)) for t in THRESHOLD_CANDIDATES]
    ok = [(t, r, p) for t, r, p in scores if p >= MIN_PRECISION]  # hit.sum()==0 이면 p=nan -> 제외
    if not ok:
        return {"threshold": None, "fallback": True, "ok_range": None, "best_range": None}
    best_recall = max(r for _, r, _ in ok)
    best = [t for t, r, _ in ok if r == best_recall]
    ts = [t for t, _, _ in ok]
    return {
        "threshold": round(float(min(best) + max(best)) / 2, 4),
        "fallback": False,
        "ok_range": (float(min(ts)), float(max(ts)), len(ts)),
        "best_range": (float(min(best)), float(max(best)), len(best)),
    }


def fit_thresholds(proba: pd.DataFrame, labels: pd.Series) -> tuple[dict[str, float | None], pd.DataFrame]:
    """부품별 임계값(fallback이면 None)과 선택 과정 표."""
    thresholds, rows = {}, []
    for c in COMPS:
        p, is_pos = proba[c].to_numpy(), (labels == c).to_numpy()
        pick = pick_threshold(p, is_pos)
        thresholds[c] = pick["threshold"]
        if pick["fallback"]:
            # argmax 규칙: 5개 클래스 중 이 부품 확률이 가장 높을 때
            hit = proba[CLASSES].to_numpy().argmax(axis=1) == CLASSES.index(c)
            tp = int((hit & is_pos).sum())
            recall, precision = tp / is_pos.sum(), (tp / hit.sum() if hit.sum() else float("nan"))
        else:
            recall, precision = recall_precision(p, is_pos, pick["threshold"])
        fmt_range = lambda r: f"{r[0]:.2f} ~ {r[1]:.2f} ({r[2]}개)" if r else "-"  # noqa: E731
        rows.append({
            "부품": c,
            "임계값": "fallback(argmax)" if pick["fallback"] else f"{pick['threshold']:.4f}",
            "검증 Recall": recall,
            "검증 Precision": precision,
            f"Precision>={MIN_PRECISION:.2f} 후보 범위": fmt_range(pick["ok_range"]),
            "최고 Recall 후보 범위": fmt_range(pick["best_range"]),
        })
    return thresholds, pd.DataFrame(rows)


def apply_rule(proba: pd.DataFrame, thresholds: dict[str, float | None]) -> pd.Series:
    """임계값을 넘은 부품 중 확률이 가장 높은 부품, 없으면 none.

    임계값이 None(fallback)인 부품은 argmax 규칙: 5개 클래스 중 그 부품 확률이 가장 높을 때 후보가 된다.
    """
    p = proba[COMPS].to_numpy()
    argmax = proba[CLASSES].to_numpy().argmax(axis=1)
    over = np.column_stack([
        argmax == CLASSES.index(c) if thresholds[c] is None else p[:, i] >= thresholds[c]
        for i, c in enumerate(COMPS)
    ])
    masked = np.where(over, p, -np.inf)
    best = masked.argmax(axis=1)
    pred = np.where(over.any(axis=1), np.array(COMPS)[best], "none")
    return pd.Series(pred, index=proba.index)


# ---------------------------------------------------------------- 4. 평가
def comp_metrics(proba: pd.DataFrame, labels: pd.Series, pred: pd.Series, thresholds: dict[str, float]) -> pd.DataFrame:
    """부품별 지표. Recall/Precision은 최종 예측 규칙 기준, PR-AUC는 부품 확률 기준 (부품 vs 나머지)."""
    rows = []
    for c in COMPS:
        actual, hit = labels == c, pred == c
        tp = int((actual & hit).sum())
        rows.append({
            "부품": c,
            "임계값": "fallback(argmax)" if thresholds[c] is None else f"{thresholds[c]:.4f}",
            "실제 고장": int(actual.sum()),
            "예측": int(hit.sum()),
            "적중": tp,
            "Recall": tp / actual.sum() if actual.sum() else float("nan"),
            "Precision": tp / hit.sum() if hit.sum() else float("nan"),
            "PR-AUC": average_precision_score(actual, proba[c]),
        })
    return pd.DataFrame(rows)


def confusion(labels: pd.Series, pred: pd.Series) -> pd.DataFrame:
    cm = confusion_matrix(labels, pred, labels=CLASSES)
    return pd.DataFrame(cm, index=[f"실제 {c}" for c in CLASSES], columns=[f"예측 {c}" for c in CLASSES])


def morning_metrics(df: pd.DataFrame, pred: pd.Series) -> dict:
    """매일 06:00 시점만 보고 '오늘 점검 대상'을 고른다고 가정한 평가."""
    at6 = df["datetime"].dt.hour == MORNING_HOUR
    labels, flagged = df.loc[at6, "label"], pred[at6] != "none"
    failing = labels != "none"
    n_days = df.loc[at6, "datetime"].dt.normalize().nunique()
    n_flag, n_fail = int(flagged.sum()), int(failing.sum())
    caught = int((flagged & failing).sum())
    caught_exact = int((flagged & (pred[at6] == labels)).sum())
    return {
        "일수": n_days,
        "06시 행 수": int(at6.sum()),
        "고위험 선별 (설비·일)": n_flag,
        "하루 평균 선별 설비 수": n_flag / n_days if n_days else float("nan"),
        "실제 24h 내 고장 (설비·일)": n_fail,
        "선별 중 실제 고장 비율 (Precision)": caught / n_flag if n_flag else float("nan"),
        "실제 고장 중 06시에 잡은 비율 (Recall)": caught / n_fail if n_fail else float("nan"),
        "실제 고장 중 부품까지 맞힌 비율": caught_exact / n_fail if n_fail else float("nan"),
    }


def feature_importance(model: lgb.LGBMClassifier, top: int = 10) -> pd.DataFrame:
    booster = model.booster_
    imp = pd.DataFrame({
        "특징": booster.feature_name(),
        "gain": booster.feature_importance("gain", iteration=model.best_iteration_),
        "split": booster.feature_importance("split", iteration=model.best_iteration_),
    })
    imp["gain 비중"] = imp["gain"] / imp["gain"].sum()
    return imp.sort_values("gain", ascending=False).head(top).reset_index(drop=True)


# ---------------------------------------------------------------- 5. 저장
def md_table(df: pd.DataFrame, index: bool = False) -> str:
    """tabulate 없이 마크다운 표를 만든다."""
    if index:
        df = df.reset_index().rename(columns={"index": ""})

    def fmt(v) -> str:
        if isinstance(v, (float, np.floating)):
            return "-" if np.isnan(v) else f"{v:.4f}"
        if isinstance(v, (int, np.integer)):
            return f"{v:,}"
        return str(v)

    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


PREV_RULE_DEFAULT = "recall>=0.80, largest threshold"  # 규칙 이름이 저장되기 전 첫 버전


def load_previous() -> dict | None:
    """이전 실행의 테스트 결과 (비교용). 같은 규칙으로 다시 돌린 경우엔 그 이전 비교 대상을 그대로 이어받는다."""
    if not MODEL_PATH.exists():
        return None
    with open(MODEL_PATH, "rb") as f:
        old = pickle.load(f)
    if old.get("threshold_rule", PREV_RULE_DEFAULT) == THRESHOLD_RULE:
        return old.get("previous")
    return {
        "rule": old.get("threshold_rule", PREV_RULE_DEFAULT),
        "thresholds": old["thresholds"],
        "test_metrics": old["results"]["test_metrics"],
        "morning": old["results"]["morning"],
    }


def comparison_tables(previous: dict, current: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    prev_m = previous["test_metrics"].set_index("부품")
    cur_m = current["test_metrics"].set_index("부품")
    comp = pd.DataFrame({
        "부품": COMPS,
        "임계값 (이전)": [prev_m.loc[c, "임계값"] for c in COMPS],
        "임계값 (변경)": [cur_m.loc[c, "임계값"] for c in COMPS],
        "Recall (이전)": [prev_m.loc[c, "Recall"] for c in COMPS],
        "Recall (변경)": [cur_m.loc[c, "Recall"] for c in COMPS],
        "Precision (이전)": [prev_m.loc[c, "Precision"] for c in COMPS],
        "Precision (변경)": [cur_m.loc[c, "Precision"] for c in COMPS],
    })
    morning = pd.DataFrame({
        "항목": list(current["morning"]),
        "이전": [previous["morning"].get(k, float("nan")) for k in current["morning"]],
        "변경": list(current["morning"].values()),
    }).astype({"이전": object, "변경": object})
    return comp, morning


def write_report(results: dict, periods: dict) -> None:
    morning = pd.DataFrame(list(results["morning"].items()), columns=["항목", "값"])
    morning["값"] = morning["값"].astype(object)
    period_rows = pd.DataFrame([{"구간": k, "시작(포함)": v["start"], "끝(제외)": v["end"]} for k, v in periods.items()])
    text = f"""# 고장 부품 예측 모델 평가 (LightGBM)

- 학습 데이터: `data/features.parquet` (설비 1대 × 1시간, 특징 {len(FEATURE_COLS)}개)
- 라벨: 24시간 안에 고장 나는 부품 (none / motor / drive / pump / bearing)
- 모델: LGBMClassifier(multiclass, class_weight=balanced), early stopping 후 트리 {results["best_iteration"]}개
- 예측 규칙: 부품별 확률이 각자의 임계값 이상인 부품 중 확률이 가장 높은 부품, 없으면 none
- 임계값: 검증 구간에서 후보 0.05~0.95(0.01 간격) 중 Precision ≥ {MIN_PRECISION:.2f}인 것만 남기고,
  Recall이 가장 높은 후보 범위의 가운데 값. 만족하는 후보가 없으면 그 부품은 argmax 규칙 (테스트 구간 미사용)

## 데이터 분할 (시간 기준)

학습·검증 구간은 라벨이 다음 구간의 고장을 보지 않도록 마지막 24시간을 제외했다.

{md_table(period_rows)}

{md_table(results["split"])}

## 부품별 임계값 선택 (검증 구간)

검증 Recall·Precision은 부품 확률 단독 기준(확률 ≥ 임계값이면 그 부품 고장으로 판단).

{md_table(results["threshold_table"])}

## 부품별 성능 (테스트 구간)

Recall·Precision은 최종 예측 규칙 기준, PR-AUC는 부품별 확률 기준(해당 부품 vs 나머지).

{md_table(results["test_metrics"])}

참고: 검증 구간 부품별 성능 (임계값을 정한 구간)

{md_table(results["valid_metrics"])}

## Confusion matrix (테스트 구간)

{md_table(results["confusion"], index=True)}

## 아침 점검 기준 평가 (테스트 구간, 매일 {MORNING_HOUR:02d}:00)

{MORNING_HOUR:02d}시 예측이 none이 아닌 설비를 "고위험(오늘 점검 대상)"으로 본다.

{md_table(morning)}

## 특징 중요도 상위 10개 (gain 기준)

{md_table(results["importance"])}
"""
    if results.get("comparison"):
        comp, morning_cmp = results["comparison"]
        text += f"""
## 임계값 규칙 변경 전후 비교 (테스트 구간)

- 이전 규칙: `{results["previous_rule"]}`
- 변경 규칙: `{THRESHOLD_RULE}`

{md_table(comp)}

아침 점검 기준 ({MORNING_HOUR:02d}:00)

{md_table(morning_cmp)}
"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- main
def main() -> None:
    pd.set_option("display.width", 200)

    log(f"데이터 읽는 중: {FEATURES_PATH.relative_to(ROOT_DIR)}")
    df = load_table()
    log(f"{len(df):,}행 x {df.shape[1]}열, {df['datetime'].min()} ~ {df['datetime'].max()}")

    print("\n== 1. 데이터 분할 ==")
    parts = split(df)
    split_table = print_split(parts)
    train, valid, test = parts["train"], parts["valid"], parts["test"]
    periods = {}
    for name in PERIODS:
        start, end = period_bounds(name)
        periods[name] = {"start": str(start), "end": str(end)}

    print("\n== 2. 학습 (검증 구간 early stopping) ==")
    log(f"학습 시작: train {len(train):,}행, valid {len(valid):,}행")
    model = train_model(train, valid)
    log(f"학습 완료: best_iteration={model.best_iteration_}")

    print("\n== 3. 부품별 임계값 (검증 구간) ==")
    valid_proba = predict_proba(model, valid)
    thresholds, threshold_table = fit_thresholds(valid_proba, valid["label"])
    valid_pred = apply_rule(valid_proba, thresholds)
    valid_metrics = comp_metrics(valid_proba, valid["label"], valid_pred, thresholds)
    print(threshold_table.to_string(index=False, float_format="{:.4f}".format))
    for c in COMPS:
        if thresholds[c] is None:
            print(f"  [fallback] {c}: Precision >= {MIN_PRECISION:.2f} 후보가 없어 argmax 규칙 사용")
    print("\n검증 구간 성능 (예측 규칙 기준):")
    print(valid_metrics.to_string(index=False, float_format="{:.4f}".format))

    print("\n== 4. 평가 (테스트 구간) ==")
    log("테스트 구간 예측 중")
    test_proba = predict_proba(model, test)
    test_pred = apply_rule(test_proba, thresholds)

    test_metrics = comp_metrics(test_proba, test["label"], test_pred, thresholds)
    print("\n[부품별 Recall / Precision / PR-AUC]")
    print(test_metrics.to_string(index=False, float_format="{:.4f}".format))

    cm = confusion(test["label"], test_pred)
    print("\n[Confusion matrix] (행: 실제, 열: 예측)")
    print(cm.to_string())

    morning = morning_metrics(test, test_pred)
    print(f"\n[아침 점검 기준: 매일 {MORNING_HOUR:02d}:00]")
    for k, v in morning.items():
        print(f"  {k:<28} {v:.4f}" if isinstance(v, float) else f"  {k:<28} {v:,}")

    importance = feature_importance(model)
    print("\n[특징 중요도 상위 10개 (gain)]")
    print(importance.to_string(index=False, float_format="{:.4f}".format))

    print("\n== 5. 저장 ==")
    results = {
        "best_iteration": model.best_iteration_,
        "split": split_table,
        "valid_metrics": valid_metrics,
        "test_metrics": test_metrics,
        "confusion": cm,
        "morning": morning,
        "importance": importance,
        "threshold_table": threshold_table,
    }
    previous = load_previous()
    if previous:
        results["previous_rule"] = previous["rule"]
        results["comparison"] = comparison_tables(previous, {"test_metrics": test_metrics, "morning": morning})
        print(f"\n[이전 규칙 대비 테스트 결과] 이전: {previous['rule']}")
        print(results["comparison"][0].to_string(index=False, float_format="{:.4f}".format))
    bundle = {
        "model": model,
        "feature_cols": list(FEATURE_COLS),
        "classes": list(CLASSES),
        "model_categories": list(MODEL_CATEGORIES),
        "thresholds": thresholds,
        "threshold_rule": THRESHOLD_RULE,  # thresholds 값이 None인 부품은 argmax 규칙 (apply_rule 참고)
        "min_precision": MIN_PRECISION,
        "periods": periods,
        "results": results,
        "previous": previous,
    }
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(bundle, f)
    log(f"모델 저장: {MODEL_PATH.relative_to(ROOT_DIR)}")

    write_report(results, periods)
    log(f"평가 보고서 저장: {REPORT_PATH.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, ml/features.py 실행 후):
#   uv add lightgbm scikit-learn   # 처음 한 번
#   uv run python ml/train.py
