"""학습된 모델(models/lgbm.pkl)로 기준 시각의 설비 100대 고장 위험을 예측한다.

- predict_df(as_of): 설비별 부품 확률 4개, top_comp, top_prob, high_risk
- 특징은 features.build_features(as_of)로 만든다 (as_of 이후 데이터는 읽지 않음)
- 고위험 판정은 train.apply_rule과 같은 규칙: 임계값 이상인 부품 중 확률이 가장 높은 부품
"""

import pickle
import sys
from functools import cache
from pathlib import Path

import pandas as pd

ML_DIR = Path(__file__).resolve().parent
ROOT_DIR = ML_DIR.parent
sys.path.insert(0, str(ML_DIR))

from features import COMPS, build_features  # noqa: E402
from train import MODEL_PATH, apply_rule  # noqa: E402

PROB_COLS = [f"p_{c}" for c in COMPS]
OUTPUT_COLS = ["machine_id", *PROB_COLS, "top_comp", "top_prob", "high_risk"]


@cache
def load_bundle() -> dict:
    """models/lgbm.pkl을 한 번만 읽는다 (프로세스 안에서 캐시)."""
    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    assert bundle["classes"][1:] == COMPS, "모델의 클래스 순서가 features.COMPS와 다릅니다"
    return bundle


def predict_df(as_of: str) -> pd.DataFrame:
    """as_of 시각 설비 100대의 부품별 고장 확률 (24시간 내)과 고위험 여부.

    top_comp: 고위험이면 규칙이 고른 부품, 아니면 확률이 가장 높은 부품
    high_risk: 임계값 이상인 부품이 하나라도 있으면 True (임계값 None인 부품은 argmax 규칙)
    """
    bundle = load_bundle()
    model, classes = bundle["model"], bundle["classes"]

    feats = build_features(as_of)
    X = feats[bundle["feature_cols"]].copy()
    # train.py와 같은 고정 범주 (model1~4)
    X["model"] = pd.Categorical(X["model"].astype(str), categories=bundle["model_categories"])

    proba = pd.DataFrame(
        model.predict_proba(X, num_iteration=model.best_iteration_),
        columns=classes, index=feats.index,
    )
    pred = apply_rule(proba, bundle["thresholds"])
    high_risk = pred != "none"
    top_comp = pred.where(high_risk, proba[COMPS].idxmax(axis=1))

    out = pd.DataFrame({"machine_id": feats["machine_id"]})
    for c, col in zip(COMPS, PROB_COLS):
        out[col] = proba[c]
    out["top_comp"] = top_comp
    out["top_prob"] = [proba.at[i, c] for i, c in top_comp.items()]
    out["high_risk"] = high_risk
    return out[OUTPUT_COLS].reset_index(drop=True)


if __name__ == "__main__":
    as_of = sys.argv[1] if len(sys.argv) > 1 else "2015-11-12 06:00:00"
    df = predict_df(as_of)
    print(f"== predict_df('{as_of}') : {len(df)}대, 고위험 {int(df['high_risk'].sum())}대 ==")
    pd.set_option("display.width", 200)
    print(df.sort_values("top_prob", ascending=False).head(10).to_string(index=False, float_format="{:.4f}".format))

# 실행 방법 (프로젝트 루트에서, ml/train.py 실행 후):
#   uv run python ml/predict.py "2015-11-12 06:00:00"
