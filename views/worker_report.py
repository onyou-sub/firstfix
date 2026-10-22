"""작업자: 주간 보고서 (우리 팀이 이번 주에 한 일을 표·그래프로 정리해 관리자에게 제출).

- 숫자·표·그래프는 store/weekly_report.build_weekly(코드 계산), 그래프·HTML은 ui/weekly_figures (내보내기와 같은 것).
- 맨 위 요약 문장만 LLM 초안 (agents/weekly_summary, 숫자가 사실과 다르면 템플릿 문장). 목업 모드는 항상 템플릿.
- 주 선택: 기준 날짜가 속한 주보다 뒤로는 못 간다.
"""
import pandas as pd
import streamlit as st

from agents import weekly_summary as wsum
from store import weekly_report as wr
from ui import components as ui
from ui import state as S
from ui import weekly_figures as wf
from ui import worker_parts as wp

ss = st.session_state
user = ss.user
team = user["team"]
day = wp.day_sidebar(team)
DAY = f"{day:%Y-%m-%d}"
ss.setdefault("wr_offset", 0)
if ss.get("wr_day") != DAY:  # 기준 날짜가 바뀌면 이번 주로
    ss.wr_day, ss.wr_offset = DAY, 0

report = wr.build_weekly(team, DAY, ss.wr_offset, author=user.get("name") or user.get("id"))
meta, week = report["meta"], report["meta"]["week_start"]
SUM_KEY, OP_KEY = f"wr_summary_{team}_{week}", f"wr_opinion_{team}_{week}"


def make_summary() -> None:
    """요약 초안 (목업은 LLM 없이 템플릿). text_area 값과 "자동 문장" 표시를 함께 바꾼다."""
    facts = report["summary_facts"]
    out = ({"text": wsum.template_summary(facts), "auto": True, "reason": "목업 모드"} if S.use_mock()
           else wsum.draft_summary(facts))
    ss[SUM_KEY], ss[f"{SUM_KEY}_auto"] = out["text"], out["auto"]


if SUM_KEY not in ss:
    with st.spinner("요약 문장 초안을 만들고 있습니다"):
        make_summary()

# ── 조작 줄 ──
st.subheader(f"주간 보고서 · {team}")
c1, c2, c3, c4, c5, c6 = st.columns([1, 1.4, 1, 1.4, 1.4, 1.4], vertical_alignment="center")
if c1.button("◀ 지난주", key="wr_prev", width="stretch"):
    ss.wr_offset -= 1
    st.rerun()
c2.markdown(f"<div style='text-align:center;font-weight:700'>{meta['week_start'][5:].replace('-', '/')} ~ "
            f"{meta['week_end'][5:].replace('-', '/')}</div>", unsafe_allow_html=True)
if c3.button("다음 주 ▶", key="wr_next", width="stretch", disabled=ss.wr_offset >= 0):
    ss.wr_offset += 1
    st.rerun()
c4.button("요약 다시 만들기", key="wr_resummary", width="stretch", on_click=make_summary,
          help="숫자는 그대로 두고 요약 문장 초안만 다시 만듭니다.")
summary_text, opinion_text = ss.get(SUM_KEY, ""), ss.get(OP_KEY, "")
page = wf.export_html(report, summary_text, opinion_text)
c5.download_button("HTML 내려받기", page, file_name=wf.file_name(report), mime="text/html", key="wr_download",
                   width="stretch", help="브라우저에서 열어 인쇄하면 PDF로 저장할 수 있습니다.")
if c6.button("관리자에게 제출", key="wr_submit", type="primary", width="stretch"):
    row = wr.submit_weekly(report, summary_text, opinion_text, page, user.get("name") or user.get("id"))
    ss.flash = f"{row['status']}했습니다 ({meta['period']})."
    st.rerun()
wp.show_flash()
if sub := wr.get_weekly(team, week):
    st.markdown(f"{ui.badge_html(sub['status'] + '됨', 'navy')} &nbsp; <span class='muted'>"
                f"{sub['submitted_at'].replace('T', ' ')[:16]}</span>", unsafe_allow_html=True)

# ── 보고서 본문 (흰 카드, 최대 900px) ──
figs = wf.figures(report)


def chart(key: str) -> None:
    if figs[key] is None:
        st.caption(wf.NO_RECORD)
        return
    st.plotly_chart(figs[key], width="stretch", key=f"wr_chart_{key}", config={"displayModeBar": False})
    st.caption(wf.CAPTIONS[key])


def table(rows: list[dict], cols: list[str] | None = None, key: str = "") -> None:
    if not rows:
        st.caption(wf.NO_RECORD)
        return
    df = pd.DataFrame(rows)
    flags = df[[c for c in ("_urgent", "_time_gap") if c in df]]
    df = df[cols or [c for c in df.columns if not c.startswith("_")]]

    def style(row):
        out = [f"background-color: {wf.URGENT_ROW_BG}" if flags.get("_urgent", pd.Series(False, index=df.index))[row.name]
               else ""] * len(row)
        if "_time_gap" in flags and flags["_time_gap"][row.name] and "실제(분)" in df:
            out[list(df.columns).index("실제(분)")] += "; font-weight: 700"
        return out

    st.dataframe(df.style.apply(style, axis=1), hide_index=True, width="stretch", key=f"wr_table_{key}",
                 column_config={"설비": st.column_config.NumberColumn(format="#%d")})


with st.container(key="weekly_doc"):
    st.markdown(f"### 주간 정비 보고서 · {team} · {meta['period']}")
    st.caption(f"작성자 {meta['author']} · 기준 날짜 {meta['day']}")

    st.markdown(f"#### {wf.SECTIONS[0]}")
    ui.kpi_cards(wf.kpi_cards(report))

    st.markdown(f"#### {wf.SECTIONS[1]}")
    st.text_area("요약", key=SUM_KEY, height=110, label_visibility="collapsed",
                 help="고친 문장이 내려받기·제출에 그대로 들어갑니다.")
    if ss.get(f"{SUM_KEY}_auto"):
        st.caption("자동 문장 (숫자로 만든 기본 문장입니다. 필요하면 고쳐 쓰세요.)")

    st.markdown(f"#### {wf.SECTIONS[2]}")
    chart("daily")

    st.markdown(f"#### {wf.SECTIONS[3]}")
    left, right = st.columns(2)
    with left:
        chart("causes")
    with right:
        chart("actions")

    st.markdown(f"#### {wf.SECTIONS[4]}")
    table(report["tasks"], wf.TASK_COLS, "tasks")
    st.caption("연한 빨강 = 긴급 작업 · 굵은 실제 시간 = 계획과 30분 이상 차이")

    st.markdown(f"#### {wf.SECTIONS[5]}")
    if report["urgent"]:
        table(report["urgent"], key="urgent")
    else:
        st.caption("이번 주 긴급 출동 없음")

    st.markdown(f"#### {wf.SECTIONS[6]}")
    chart("time")

    st.markdown(f"#### {wf.SECTIONS[7]}")
    left, right = st.columns(2)
    with left:
        st.markdown("**특이사항**")
        table(report["notes"], key="notes")
    with right:
        st.markdown("**사용 부품**")
        table(report["parts"], key="parts")

    st.markdown(f"#### {wf.SECTIONS[8]}")
    left, right = st.columns(2)
    with left:
        st.markdown("**이월 작업**")
        table(report["next_week"]["carryover"], key="carry")
    with right:
        st.markdown("**주의 설비** (이번 주 2회 이상 작업)")
        table(report["next_week"]["attention"], key="attention")

    st.markdown(f"#### {wf.SECTIONS[9]}")
    st.text_area("작업자 의견 (선택)", key=OP_KEY, height=90, label_visibility="collapsed",
                 placeholder="관리자에게 전할 말이 있으면 적어 주세요 (선택).")
