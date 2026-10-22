"""작업자: 작업 이력 (우리 팀의 완료 보고 목록) + 자주 손본 설비 Top 5."""

import pandas as pd
import streamlit as st

from config import COMP_LABEL_KO
from store import app_db
from store import worker_stats as ws
from ui import worker_parts as wp
from ui.style import RED

user = st.session_state.user
team = user["team"]
day = wp.day_sidebar(team)
DAY = f"{day:%Y-%m-%d}"

st.subheader(f"작업 이력 · {team}")
reports = app_db.work_reports(team=team)
if not reports:
    st.info("아직 완료 보고가 없습니다.")
else:
    df = pd.DataFrame([{
        "날짜": r["as_of_date"], "작업 지시": r["wo_id"], "설비": r["machine_id"],
        "예측 부품": COMP_LABEL_KO.get(r["predicted_comp"], r["predicted_comp"]),
        "실제 원인": r["actual_cause_type"], "실제 부품": COMP_LABEL_KO.get(r["actual_comp"], r["actual_comp"]),
        "조치": r["action"], "소요(분)": r["duration_min"], "사용 부품": r["parts_used"],
        "보고자": r["reported_by"], "보고 시각": r["reported_at"].replace("T", " "), "메모": r["memo"] or "-",
    } for r in reports])
    st.caption(f"{len(df)}건")
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"설비": st.column_config.NumberColumn(format="#%d")})

# ---------------------------------------------------------------- 자주 손본 설비 Top 5 (기준 날짜까지의 완료 보고)
st.markdown("#### 자주 손본 설비 Top 5")
period, scope = wp.stats_filters("hist")
top = wp.cached_stat("top_machines", DAY, team, scope, period)
if top.empty:
    st.info(ws.NO_REPORTS)
else:
    t = pd.DataFrame({"순위": range(1, len(top) + 1), "설비": top["machine_id"], "작업 수": top["work_count"],
                      "주요 원인": top["main_cause"],
                      "주요 부품": [COMP_LABEL_KO.get(c, c or "-") for c in top["main_comp"]],
                      "마지막 작업일": top["last_date"], "미완료": top["open_count"]})
    styled = t.style.map(lambda v: f"color: {RED}; font-weight: 700" if v >= 1 else "", subset=["미완료"])
    st.dataframe(styled, hide_index=True, width="stretch", column_config={
        "설비": st.column_config.NumberColumn(format="#%d"),
        "작업 수": st.column_config.ProgressColumn(format="%d건", min_value=0, max_value=int(t["작업 수"].max())),
        "미완료": st.column_config.NumberColumn(format="%d건", help="이 설비의 우리 팀 미완료 작업 수")})
st.caption(f"{ws.period_text(period)} 완료 보고 기준 · 같은 설비에 작업이 반복되면 근본 원인 점검을 고려하세요.")
