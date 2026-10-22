"""이력·보고서: 주간 보고서(작업자 제출) / 승인 내역 / 보고서 / 예측 검증 (결과 파일만 읽는다)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import records
from store import weekly_report as wr
from ui import components as ui
from ui import state as S

ss = st.session_state
S.init()
STATUSES = ["전체", "승인", "거절", "대체됨", "대기"]
KINDS = ["전체", "작업 지시", "발주"]
EMPTY = "아직 처리 내역이 없습니다."


@st.cache_data(ttl=60, show_spinner="검증 결과 계산 중")
def cached_verification(start: date, end: date) -> pd.DataFrame:
    return records.verification(start, end)


st.subheader("이력·보고서")
table, raw = records.decisions()

with st.container(border=True):
    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    period = f1.date_input("기간", value=(S.TEST_START, S.TEST_END), min_value=S.TEST_START, max_value=S.TEST_END,
                           key="hist_period")
    status = f2.selectbox("상태", STATUSES, key="hist_status")
    kind = f3.selectbox("유형", KINDS, key="hist_kind")
    machines = sorted({m for ms in table["설비 목록"] for m in ms})
    machine = f4.selectbox("설비 번호", ["전체", *machines], key="hist_machine",
                           format_func=lambda m: m if m == "전체" else f"#{m}")
start, end = (period[0], period[1]) if len(period) == 2 else (period[0], period[0])

tab_weekly, tab1, tab2, tab3 = st.tabs(["주간 보고서", "승인 내역", "보고서", "예측 검증"], key="history_tab",
                                      on_change="rerun")

with tab1:
    view = records.filter_decisions(table, start, end, status, kind, None if machine == "전체" else machine)
    view = view[[c for c in view.columns if c != "설비 목록"]]
    if view.empty:
        st.info(EMPTY)
    else:
        st.caption(f"{len(view)}건 · 행을 선택하면 아래에 상세가 표시됩니다.")
        event = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                             key="hist_table")
        st.download_button("CSV 내려받기", view.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"approvals_{start:%Y%m%d}_{end:%Y%m%d}.csv", mime="text/csv", key="hist_csv")
        rows = event.selection.rows if event is not None else []
        if rows:
            row = view.iloc[rows[0]]
            d = raw[row["초안 번호"]]
            with st.container(border=True):
                st.markdown(f"**{row['초안 번호']}** · {row['유형']} · 설비 {row['설비']} &nbsp; "
                            f"{ui.badge_html(row['상태'])}", unsafe_allow_html=True)
                if d.get("title"):
                    st.markdown(d["title"])
                if d.get("checklist"):
                    st.markdown("**체크리스트**")
                    st.markdown("\n".join(f"{i}. {c}" for i, c in enumerate(d["checklist"], 1)))
                if d["type"] == "purchase_request":
                    st.markdown(f"**발주** {d.get('part_name', d.get('comp'))} {d.get('qty')}개 · 현재 재고 "
                                f"{d.get('current_qty')} · 희망 발주일 {row['희망 발주일']} · 입고 소요 {d.get('lead_time_days')}일")
                    if d.get("note"):
                        st.caption(f"메모: {d['note']}")
                st.markdown("**근거 요약**")
                for eid, summary in records.evidence_summaries(d):
                    st.markdown(f"- 근거 번호 {eid}: {summary}")
                rep = records.work_report(row["초안 번호"])
                if rep:
                    st.markdown("**작업자 완료 보고** (제목을 누르면 메모 전체)")
                    ui.report_toggles([rep], open_wo=rep["wo_id"])

with tab2:
    days = [d for d in records.report_days() if start <= d <= end]
    if not days:
        st.info("기간 안에 저장된 보고서가 없습니다.")
    else:
        day = st.selectbox("날짜", days, format_func=lambda d: f"{d:%Y-%m-%d}", key="hist_report_day")
        body = records.read_report(day)
        with st.container(border=True):
            st.markdown(body or "보고서를 읽지 못했습니다.")
        if body:
            st.download_button("보고서 내려받기 (.md)", body, file_name=f"handover_{day:%Y%m%d}.md",
                               key="hist_report_download")

with tab_weekly:  # 작업자가 제출한 팀별 주간 보고서
    weekly = wr.list_weekly()
    if not weekly:
        st.info("아직 제출된 주간 보고서가 없습니다.")
    else:
        wt = pd.DataFrame([{"팀": r["team"], "주": f"{r['week_start']} 주", "작성자": r["author"] or "-",
                            "상태": r["status"], "제출 시각": r["submitted_at"].replace("T", " ")[:16]} for r in weekly])
        picked = st.dataframe(wt, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                              key="hist_weekly_table")
        rows = picked.selection.rows if picked is not None else []
        if not rows:
            st.caption("행을 고르면 보고서가 아래에 열립니다.")
        else:
            row = weekly[rows[0]]
            page = wr.read_html(row)
            if page is None:
                st.error("보고서 파일을 읽지 못했습니다.")
            else:
                st.download_button("보고서 내려받기 (.html)", page, mime="text/html", key="hist_weekly_download",
                                   file_name=f"주간보고서_{row['team']}_{row['week_start']}.html")
                components.html(page, height=2200, scrolling=True)

with tab3:
    try:
        vt = cached_verification(start, end)
    except Exception as e:
        vt = None
        st.error(f"검증 실패: {type(e).__name__}: {e}")
    if vt is not None:
        if vt.empty:
            st.info("기간 안에 예측 기록이 없습니다. 오늘의 분석에서 점검을 실행하면 예측이 저장됩니다.")
        else:
            total = vt[["고위험", "적중", "헛경보", "놓침"]].sum()
            ui.kpi_cards([("기간 고위험", int(total["고위험"]), None), ("적중", int(total["적중"]), None),
                          ("헛경보", int(total["헛경보"]), None), ("놓침", int(total["놓침"]), None)])
            st.dataframe(vt, hide_index=True, width="stretch",
                         column_config={"날짜": st.column_config.DateColumn(format="YYYY-MM-DD")})
            st.caption("날짜별 06시 예측의 고위험 설비와 그 뒤 24시간 실제 고장을 설비 단위로 비교합니다.")
