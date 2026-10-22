"""알림 전체 보기 (관리자·작업자): 사이드바에서 숨긴 알림(읽음, 이전 날짜, 처리된 긴급)까지 모두.

- 필터: 기간(기준 날짜 / 최근 7일 / 전체), 종류, 안 읽은 것만
- 표에서 행을 고르면 [열기](읽음 처리 후 연결 작업으로 이동) / [읽음 처리]
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from store import app_db
from ui import components as ui
from ui import state as S

ss = st.session_state
S.init()
user = ss.user
TARGET = "views/worker_tasks.py" if user["role"] == "worker" else "views/work_status.py"
PERIODS = ["기준 날짜", "최근 7일", "전체"]
KIND_OPTIONS = {"전체": None, **{app_db.KIND_LABEL[k]: k for k in app_db.KINDS}}

st.subheader("알림")
f1, f2, f3 = st.columns([2, 1.2, 1], vertical_alignment="bottom")
period = f1.radio("기간", PERIODS, horizontal=True, key="noti_period")
kind = KIND_OPTIONS[f2.selectbox("종류", list(KIND_OPTIONS), key="noti_kind")]
unread_only = f3.checkbox("안 읽은 것만", key="noti_unread")

day = f"{ss.day:%Y-%m-%d}"
since = f"{ss.day - timedelta(days=6):%Y-%m-%d}" if period == "최근 7일" else None
rows = app_db.notification_history(user, day=day if period == "기준 날짜" else None, kind=kind,
                                   unread_only=unread_only, since=since)

if not rows:
    st.info("조건에 맞는 알림이 없습니다.")
else:
    st.caption(f"{len(rows)}건 · 사이드바에서 사라진 알림(읽음, 이전 날짜, 처리된 긴급)도 여기서는 모두 보입니다.")
    table = pd.DataFrame([{  # 표 안에는 HTML 배지를 넣을 수 없어 종류는 배지 이름 글자로
        "시각": r["created_at"].replace("T", " ")[:16], "종류": r["label"], "설비": r["head"] or "-",
        "내용": r["line"] or "", "연결 작업": r["link_wo_id"] or "-", "읽음": "읽음" if r["read"] else "안 읽음",
    } for r in rows])
    event = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="noti_table")
    picked = event.selection.rows if event and event.selection else []
    sel = rows[picked[0]] if picked else None
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("열기", key="notipage_open", disabled=sel is None or not sel["link_wo_id"],
                 help="읽음 처리 후 연결 작업 화면으로 이동합니다."):
        ui._open_notification(sel, user, TARGET)
    if b2.button("읽음 처리", key="notipage_mark", disabled=sel is None or sel["read"]):
        app_db.mark_read(sel["id"], user)
        st.rerun()
