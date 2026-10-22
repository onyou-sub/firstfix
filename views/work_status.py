"""관리자: 작업 현황 (작업 지시 상태, 팀별 간트, 완료 보고, 예측 원인 적중률, 발주 입고)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from config import COMP_LABEL_KO
from ops_settings import load_settings
from store import app_db
from ui import components as ui
from ui import state as S

ss = st.session_state
S.init()
CFG = load_settings()


def _minutes(hm: str | None) -> int | None:
    if not hm:
        return None
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


focus = app_db.get_work_order(ss.focus_wo) if ss.get("focus_wo") else None
if focus and ss.get("ws_focus_applied") != focus["wo_id"]:  # 알림으로 처음 열었을 때만 날짜를 맞춘다
    ss.ws_day = date.fromisoformat(focus["as_of_date"])
    ss.ws_focus_applied = focus["wo_id"]
ss.setdefault("ws_day", ss.day)

st.subheader("작업 현황")
c1, _ = st.columns([1, 3])
day = c1.date_input("기준 날짜", min_value=S.TEST_START, max_value=S.TEST_END, key="ws_day")
day_text = f"{day:%Y-%m-%d}"
orders = app_db.work_orders(day_text)
reports = app_db.work_reports(as_of_date=day_text)

if focus:
    with st.container(border=True):
        rep = next((r for r in app_db.work_reports() if r["wo_id"] == focus["wo_id"]), None)
        st.markdown(f"**알림에서 연 작업** · {focus['wo_id']} {focus['title']} ({focus['team']} "
                    f"{focus['start']}~{focus['end']}) &nbsp; {ui.badge_html(app_db.WO_STATUS_KO[focus['status']])}",
                    unsafe_allow_html=True)
        if rep:  # 메모 전체를 펼친 상태로
            ui.report_toggles([rep], open_wo=rep["wo_id"])
        if st.button("닫기", key="ws_focus_close"):
            ss.focus_wo = None
            st.rerun()

@st.fragment(run_every=60)
def status_gantt_section(day: date) -> None:
    """작업 지시 간트 + "지금" 세로선 (기준 날짜를 볼 때만, 근무 시간 밖이면 없음). 1분마다 다시 그린다."""
    import live_ops

    fig = ui.status_gantt(app_db.work_orders(f"{day:%Y-%m-%d}"), day, CFG)
    if fig is None:
        st.info("이 날짜에 승인된 작업 지시가 없습니다.")
        return
    if day == ss.day:
        ui.add_now_line(fig, day, CFG, live_ops.NOW_HM(CFG))
    st.plotly_chart(fig, width="stretch", key="status_gantt")


with st.container(border=True):
    st.markdown("**오늘 작업현황**")
    counts = {s: sum(w["status"] == s for w in orders) for s in app_db.WO_STATUS}
    ui.kpi_cards([(app_db.WO_STATUS_KO[s], f"{n}건", None) for s, n in counts.items()])
    status_gantt_section(day)

with st.container(border=True):
    st.markdown("**완료 보고**")
    if not reports:
        st.info("아직 완료 보고가 없습니다.")
    else:
        by_id = {w["wo_id"]: w for w in orders}
        rows = []
        for r in reports:
            w = by_id.get(r["wo_id"], {})
            planned = (_minutes(w.get("end")) - _minutes(w.get("start"))) if w.get("start") and w.get("end") else None
            rows.append({"설비": r["machine_id"], "팀": r["team"],
                         "예측 부품": COMP_LABEL_KO.get(r["predicted_comp"], r["predicted_comp"]),
                         "실제 원인 유형": r["actual_cause_type"], "조치": r["action"],
                         "예측 소요(분)": planned, "실제 소요(분)": r["duration_min"], "보고자": r["reported_by"]})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"설비": st.column_config.NumberColumn(format="#%d")})
        st.markdown("**작업자 메모** (제목을 누르면 메모 전체)")
        ui.report_toggles(reports, open_wo=ss.get("focus_wo"))
    all_reports = app_db.work_reports()
    if all_reports:
        hit = sum(r["actual_cause_type"] == "예측과 같음" for r in all_reports)
        ui.kpi_cards([("예측 원인 적중률 (전체)", f"{hit / len(all_reports):.0%}", "완료 보고 중 '예측과 같음' 비율"),
                      ("완료 보고 (전체)", f"{len(all_reports)}건", None),
                      ("이 날짜 완료 보고", f"{len(reports)}건", None)])

with st.container(border=True):
    st.markdown("**발주**")
    pos = app_db.purchase_orders(day_text)
    if not pos:
        st.caption("이 날짜에 승인된 발주가 없습니다.")
    for po in pos:
        cols = st.columns([3, 1])
        status = "입고" if po["status"] == "RECEIVED" else "발주 요청"
        cols[0].markdown(f"{po['po_id']} · {COMP_LABEL_KO.get(po['comp'], po['comp'])} {po['qty']}개 "
                         f"{ui.badge_html(status, 'navy' if po['status'] == 'RECEIVED' else 'orange')}",
                         unsafe_allow_html=True)
        if po["status"] == "REQUESTED" and cols[1].button("입고 처리", key=f"recv_{po['po_id']}",
                                                         help="재고에 수량을 더합니다 (이 날짜 18:00 기준으로 기록)."):
            app_db.receive_purchase_order(po["po_id"], f"{day_text} 18:00:00")
            st.rerun()
