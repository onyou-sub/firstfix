"""작업자: 오늘 작업 (우리 팀의 기준 날짜 작업 지시를 시간순 카드로, 작업 시작·완료 보고).

긴급 점검(경고 자동 배정, urgent=1)은 맨 위 [출동 확인] 상자 + 목록 맨 위 "긴급" 배지·출동 시각 강조.
이전 날짜에서 밀린 작업은 대시보드 달력에서 본다 (여기서는 건수만 한 줄로 안내).
"""
from __future__ import annotations

import streamlit as st

from store import app_db
from ui import components as ui
from ui import worker_parts as wp

ss = st.session_state
team = ss.user["team"]
day = wp.day_sidebar(team)

st.subheader(f"오늘 작업 · {team} · {day:%Y-%m-%d}")
orders, carried, waiting = wp.waiting_urgent(f"{day:%Y-%m-%d}", team)
if carried:
    st.info(f"밀린 작업 {len(carried)}건 — 대시보드 달력에서 확인")
counts = {s: sum(w["status"] == s for w in orders) for s in app_db.WO_STATUS}
ui.kpi_cards([(app_db.WO_STATUS_KO[s], f"{n}건", None) for s, n in counts.items()])

wp.show_flash()
wp.urgent_box(waiting)

if not orders:
    st.info(f"{day:%Y-%m-%d}에 배정된 {team} 작업이 없습니다.")
for wo in orders:
    wp.order_card(wo)

# 알림에서 연 작업이 위 목록에 없으면 (이전 날짜 미완료, 다른 날짜의 완료 작업 등) 따로 보여 준다
focus = app_db.get_work_order(ss.focus_wo) if ss.get("focus_wo") else None
if focus and focus["team"] == team and focus["wo_id"] not in {w["wo_id"] for w in orders}:
    st.markdown("**알림에서 연 작업**")
    wp.order_card(focus, show_date=True)
