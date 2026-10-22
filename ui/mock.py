"""가짜 엔진 (USE_MOCK=True일 때만 사용, LLM 호출 없음). 기존 app.py의 mock_* 함수를 그대로 옮겼다."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st

N_MACHINES = 100
CREWS = 3
THRESHOLD = 0.5            # 이 확률 이상이면 고위험
MAX_TOOL_CALLS = 6         # Investigator당 도구 호출 상한
TEST_START, TEST_END = date(2015, 10, 1), date(2015, 12, 31)
DEMO_DATES = "10/06 (고위험 0대) · 10/01 (5대 적중) · 12/22 (13번 교체 직후 충돌)"

COMP = {  # 가상 매핑 (데이터에는 comp1~4라는 이름만 있음) — 목업 전용
    "comp1": ("모터/전원부", "volt", "error1"),
    "comp2": ("구동부", "rotate", "error2"),
    "comp3": ("밸브/펌프", "pressure", "error4"),
    "comp4": ("베어링", "vibration", "error5"),
}
SENSOR_BASE = {"volt": 170.8, "rotate": 446.6, "pressure": 100.9, "vibration": 40.4}
CRIT_WEIGHT = {"A": 3, "B": 2, "C": 1}
MANUAL = {
    "comp1": ("manual_motor.md §3.2", "전압이 기준 대비 10% 이상 높게 24시간 지속되면 전원부 단자 체결 상태와 절연 저항을 먼저 확인한다."),
    "comp2": ("manual_drive.md §2.4", "회전수가 평소보다 15% 이상 떨어지면 감속기 오일 레벨과 커플링 마모를 점검한다."),
    "comp3": ("manual_pump.md §4.1", "토출 압력 상승이 지속되면 밸브 시트 이물과 필터 막힘 여부를 확인한다."),
    "comp4": ("manual_bearing.md §1.3", "진동이 기준 대비 20% 이상 증가하면 베어링 윤활 상태와 하우징 온도를 측정한다."),
    "sensor": ("manual_sensor.md §2.1", "부품 교체 직후 같은 이상이 계속되면 센서 고정 상태와 배선 접촉을 먼저 확인한다."),
}


def rng_for(as_of: datetime, salt: int = 0) -> np.random.Generator:
    return np.random.default_rng(int(as_of.strftime("%Y%m%d")) * 10 + salt)


@st.cache_data
def machine_master() -> pd.DataFrame:
    """machines.csv + 가상 중요도 CSV를 흉내 냅니다."""
    r = np.random.default_rng(7)
    return pd.DataFrame({
        "machine_id": range(1, N_MACHINES + 1),
        "model": r.choice(["model1", "model2", "model3", "model4"], N_MACHINES, p=[.16, .17, .35, .32]),
        "age": r.integers(0, 21, N_MACHINES),
        "criticality": r.choice(["A", "B", "C"], N_MACHINES, p=[.2, .5, .3]),
    })


def parts_stock(as_of: datetime) -> dict:
    """가상 재고 CSV. 날짜마다 한 부품은 재고 0이 되도록 해서 발주 흐름을 볼 수 있게 함."""
    r = rng_for(as_of, 3)
    stock = {c: int(r.integers(1, 5)) for c in COMP}
    stock[r.choice(list(COMP))] = 0
    return stock


def mock_predict(as_of: datetime) -> pd.DataFrame:
    """[ML] predict_failure(as_of) — 실제로는 LightGBM이 100대 × 5클래스 확률을 계산."""
    r = rng_for(as_of)
    df = machine_master().copy()
    df["comp"] = r.choice(list(COMP), N_MACHINES)
    df["prob"] = np.round(r.beta(1.2, 12, N_MACHINES), 2)
    k = 0 if as_of.day % 7 == 0 else int(r.integers(2, 7))   # 7의 배수 날은 고위험 0대
    idx = r.choice(N_MACHINES, k, replace=False)
    df.loc[idx, "prob"] = np.round(r.uniform(0.55, 0.95, k), 2)
    df["high_risk"] = df["prob"] >= THRESHOLD
    return df


def mock_investigate(row: pd.Series, as_of: datetime) -> dict:
    """[Investigator] 설비 1대 조사. 실제로는 create_agent + 조회 도구 6종, 호출 상한 6회."""
    r = rng_for(as_of, int(row.machine_id))
    comp = row.comp
    name, sensor, err = COMP[comp]
    recent_replace = r.random() < 0.2                       # 20% 확률로 "최근 교체" 상황 연출
    days_since = int(r.integers(1, 6)) if recent_replace else int(r.integers(20, 200))

    hours = pd.date_range(as_of - timedelta(hours=71), as_of, freq="h")
    base = SENSOR_BASE[sensor]
    noise = r.normal(0, base * 0.02, len(hours))
    ramp = np.clip(np.arange(len(hours)) - 40, 0, None) * base * (0.007 if sensor != "rotate" else -0.006)
    trend = pd.DataFrame({"datetime": hours, "value": base + noise + ramp})
    mean24 = trend["value"].tail(24).mean()
    err_cnt = int(r.integers(1, 4))

    tool_calls = [
        ("get_sensor_trend", f"machine_id={row.machine_id}, sensor='{sensor}', hours=72",
         f"24h 평균 {mean24:.1f} (전체 평균 {base}, {((mean24 / base) - 1) * 100:+.0f}%)"),
        ("get_error_logs", f"machine_id={row.machine_id}, days=7", f"{err} {err_cnt}건"),
        ("get_maintenance_history", f"machine_id={row.machine_id}", f"{comp} 마지막 교체 {days_since}일 전"),
        ("find_similar_failures", f"model='{row.model}', comp='{comp}'", "유사 사례 3건 (고장 전 같은 센서 상승)"),
        ("search_manual", f"query='{name} {sensor} 상승 점검'", f"[출처: {MANUAL[comp][0]}]"),
    ]
    if recent_replace:
        tool_calls.append(("search_manual", "query='교체 직후 이상 센서 점검'", f"[출처: {MANUAL['sensor'][0]}]"))
    tool_calls = tool_calls[:MAX_TOOL_CALLS]

    if recent_replace:
        label = "센서·설치 이상 가능성"
        summary = (f"{name}({comp})를 {days_since}일 전에 교체했는데 예측은 고위험입니다. "
                   "부품 자체보다 센서 고정·설치 상태를 사람이 확인해야 합니다.")
        manual = MANUAL["sensor"]
    else:
        label = "예측 고장"
        summary = (f"{sensor} 24h 평균이 평소보다 {abs(mean24 / base - 1) * 100:.0f}% "
                   f"{'낮고' if sensor == 'rotate' else '높고'}, {err} {err_cnt}건이 발생했습니다. "
                   f"{name}({comp}) 고장 전 패턴과 같습니다.")
        manual = MANUAL[comp]

    return {
        "machine_id": int(row.machine_id), "comp": comp, "comp_name": name, "sensor": sensor,
        "prob": float(row.prob), "criticality": row.criticality, "label": label,
        "summary": summary, "days_since": days_since, "trend": trend, "base": base,
        "tool_calls": tool_calls, "manual": manual,
        "evidence_id": f"E{as_of:%m%d}-{int(row.machine_id):03d}",
        # 짧은 요약 필드 (실제 Finding과 같은 키)
        "anomaly_summary": f"{sensor} 24h 평균 {mean24:.1f}(평소 대비 {(mean24 / base - 1) * 100:+.0f}%)",
        "hypotheses": ([{"cause": "센서·설치 이상", "evidence": f"{comp} {days_since}일 전 교체"}] if recent_replace
                       else [{"cause": f"{name} 열화", "evidence": f"{err} {err_cnt}건, {sensor} 상승"}]),
        "action_summary": "센서 고정·배선부터 확인" if recent_replace else f"{name} 점검 후 이상 시 교체",
        "steps": [{"action": "설비 정지·잠금 확인", "criterion": "잠금 태그 부착", "if_abnormal": "작업 중지"},
                  {"action": f"{sensor} 값 재측정", "criterion": "평소 수준과 비교", "if_abnormal": "부품 점검"},
                  {"action": f"{name} 상태 확인", "criterion": "매뉴얼 기준", "if_abnormal": "교체 검토"}],
        "field_tips": [], "est_minutes": 60, "parts_tools": [], "key_evidence": [], "notes": [],
    }


def mock_plan(findings: list[dict], as_of: datetime) -> tuple[pd.DataFrame, list[dict]]:
    """[Planner] rank_maintenance(코드 계산) + draft_* 도구로 초안 작성. 등록은 하지 않음."""
    stock = parts_stock(as_of)
    rows = []
    for f in findings:
        s = stock[f["comp"]]
        score = f["prob"] * CRIT_WEIGHT[f["criticality"]] * (1.0 if s > 0 else 0.8)
        rows.append({**{k: f[k] for k in ("machine_id", "comp", "comp_name", "prob", "criticality", "label")},
                     "stock": s, "score": round(score, 2)})
    plan = pd.DataFrame(rows)
    if plan.empty:
        return plan, []
    plan = plan.sort_values("score", ascending=False).reset_index(drop=True)
    plan["slot"], plan["task"], plan["reason"] = "내일 우선", "교체", "목업 배정"
    plan["team"], plan["start"], plan["end"], plan["duration_min"] = None, None, None, 120
    team = 1
    for i, p in plan.iterrows():
        if p.label != "예측 고장":
            plan.at[i, "slot"], plan.at[i, "task"], plan.at[i, "duration_min"] = "사람 확인", "센서 점검", 60
        elif team <= CREWS:
            plan.at[i, "slot"] = f"{team}팀 09:00-11:00"
            plan.at[i, "team"], plan.at[i, "start"], plan.at[i, "end"] = f"{team}팀", "09:00", "11:00"
            team += 1

    actions = {f["machine_id"]: f["action_summary"] for f in findings}
    drafts = []
    for _, p in plan.iterrows():
        if p.team:
            drafts.append({"id": f"WO-{as_of:%m%d}-{p.machine_id:03d}", "type": "작업 지시서",
                           "title": f"설비 #{p.machine_id} {p.comp_name}({p.comp}) 점검·교체 — {p.slot}",
                           "body": f"체크리스트: 1) 설비 정지 절차 확인 2) {MANUAL[p.comp][1]} 3) 부품 교체 후 24시간 센서 확인",
                           "status": "PENDING", "version": 1,
                           "machine": f"#{p.machine_id} {p.comp_name}", "task": p.task, "when": p.slot,
                           "todo": actions.get(p.machine_id, "-"),
                           "team": p.team, "start": p.start, "group": p.team})
        if p.stock == 0 and p.label == "예측 고장":
            drafts.append({"id": f"PR-{as_of:%m%d}-{p.comp}", "type": "발주 요청",
                           "title": f"{p.comp_name}({p.comp}) 2개 발주 — 재고 0",
                           "body": "사유: 24시간 내 고장 예측 설비가 있으나 재고가 없음. 입고 소요 3일(가정).",
                           "status": "PENDING", "version": 1,
                           "machine": f"#{p.machine_id} {p.comp_name}", "task": f"발주 {p.comp_name} 2개",
                           "when": f"희망 발주일 {as_of:%Y-%m-%d}", "todo": "오늘 발주 → 예상 입고 3일 뒤 (가정)",
                           "team": None, "start": None, "group": "발주"})
    drafts = list({d["id"]: d for d in drafts}.values())
    return plan, drafts


def mock_actual_failures(pred: pd.DataFrame, as_of: datetime) -> set[int]:
    """[검증 전용] as_of 이후 24시간의 실제 고장. 에이전트 도구에는 없는 기능."""
    r = rng_for(as_of, 99)
    hit = {int(m) for m, p in zip(pred.machine_id, pred.prob) if p >= THRESHOLD and r.random() < p}
    miss = {int(m) for m in r.choice(pred[~pred.high_risk].machine_id, int(r.integers(0, 2)), replace=False)}
    return hit | miss
