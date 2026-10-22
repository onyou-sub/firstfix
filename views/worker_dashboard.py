"""작업자: 대시보드 (첫 화면). LLM 없음, 집계는 store/worker_stats.py.

위에서부터: 긴급 [출동 확인] 상자 → 숫자 카드 3개 → ① 오늘 시간표 → ② 달력(월간) + 선택 날짜 작업 목록
→ ③ 내 설비 실시간 센서(2초마다 이 구역만 갱신) → 기간·범위 필터 → ④ 점검 결과 분포 · ⑤ 조치 분포.
시간표 칸·달력 이벤트를 누르면 ② 목록에서 그 작업을 펼친다 (ss.open_wo).
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_calendar import calendar

import ops_settings
import services
from store import app_db
from store import worker_stats as ws
from tools.data_tools import SENSOR_MEAN
from tools.plan_tools import TASK_LABEL, load_work_standards
from ui import components as ui
from ui import worker_parts as wp
from ui.style import GRAY, NAVY, ORANGE, RED

ss = st.session_state
user = ss.user
team = user["team"]
day = wp.day_sidebar(team)
DAY = f"{day:%Y-%m-%d}"
AS_OF = app_db.report_as_of(DAY)  # 데모 기준 시각 (매일 06시, 1초 스트림도 이 시각부터 흐른다)
STATUS_COLOR = {"배정": GRAY, "진행 중": ORANGE, "완료": NAVY}
CAUSE_COLOR = {"예측과 같음": NAVY, "다른 부품": ORANGE, "센서·설치 문제": "#F2C08F", "이상 없음": GRAY}
NAVY_LIGHT = "#8FA3BF"
SENSOR_KO = {"volt": "전압", "rotate": "회전수", "pressure": "압력", "vibration": "진동"}
CHART_CONFIG = {"displayModeBar": False}


def duration_text(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}시간 {m}분" if h and m else f"{h}시간" if h else f"{m}분"


@st.cache_data(show_spinner=False)
def _standards() -> dict:
    try:
        return load_work_standards()
    except Exception:  # pdm.db가 없어도 화면은 열린다 (지시 시각으로 계산)
        return {}


def standard_minutes(wo: dict, settings: dict) -> int:
    """작업 표준 시간 (work_standards → 긴급 점검 설정 → 지시 시각 → 60분)."""
    std = _standards()
    hit = std.get((wo["task_type"], wo["comp"])) or std.get((wo["task_type"], ""))
    if hit:
        return hit["duration_min"]
    if wo["task_type"] == "urgent_check":
        return int(settings["urgent_check_min"])
    if wo["start"] and wo["end"]:
        return wp._minutes(wo["end"]) - wp._minutes(wo["start"])
    return 60


# ---------------------------------------------------------------- (0) 긴급 상자 · 숫자 카드
orders, carried, waiting = wp.waiting_urgent(DAY, team)
settings = ops_settings.load_settings()
st.subheader(f"대시보드 · {team} · {DAY}")
wp.show_flash()
wp.urgent_box(waiting)

done = sum(w["status"] == "DONE" for w in orders)
left_min = sum(standard_minutes(w, settings) for w in orders if w["status"] != "DONE")
ui.kpi_cards([("오늘 완료", f"{done}/{len(orders)}", "기준 날짜에 지시된 우리 팀 작업 중 완료한 수"),
              ("남은 작업 시간", duration_text(left_min), "오늘 남은 작업의 표준 작업 시간 합"),
              ("밀린 작업", f"{len(carried)}건", "기준 날짜 전에 지시됐지만 아직 완료하지 않은 작업")])

if ss.get("cal_for_day") != DAY:  # 기준 날짜가 바뀌면 가장 오래된 밀린 날짜부터 다시
    ss.cal_for_day, ss.cal_date, ss.open_wo = DAY, ws.default_calendar_date(DAY, team), None


# ---------------------------------------------------------------- ① 오늘 시간표
def timeline_figure(tl: pd.DataFrame) -> go.Figure:
    df = tl.dropna(subset=["start_dt", "end_dt"]).copy()
    df["row"] = "오늘"
    df["status_ko"] = df["status"].map(app_db.WO_STATUS_KO)
    df["label"] = [("긴급 " if u else "") + f"#{m} {ws.COMP_SHORT.get(c, c)} {ws.TASK_SHORT.get(t, '작업')}"
                   for u, m, c, t in zip(df["urgent"], df["machine_id"], df["comp"], df["task_type"])]
    fig = px.timeline(df, x_start="start_dt", x_end="end_dt", y="row", color="status_ko", text="label",
                      custom_data=["wo_id", "urgent"], color_discrete_map=STATUS_COLOR,
                      category_orders={"status_ko": list(STATUS_COLOR)})
    for tr in fig.data:  # 긴급 작업은 빨간 칸
        urg = [bool(c[1]) for c in tr.customdata]
        if any(urg):
            base = STATUS_COLOR.get(tr.name, GRAY)
            tr.marker.color = [RED if u else base for u in urg]
        tr.textposition, tr.insidetextanchor, tr.textfont.color = "inside", "middle", "#FFFFFF"
        tr.hovertemplate = "%{text}<br>%{base|%H:%M}~%{x|%H:%M}<extra></extra>"
    x0 = min([pd.Timestamp(f"{DAY} {settings['work_start']}"), *df["start_dt"]])
    x1 = max([pd.Timestamp(f"{DAY} {settings['work_end']}"), *df["end_dt"]])
    fig.add_vrect(x0=pd.Timestamp(f"{DAY} {settings['lunch_start']}"), x1=pd.Timestamp(f"{DAY} {settings['lunch_end']}"),
                  fillcolor="#E5E8ED", opacity=0.6, layer="below", line_width=0,
                  annotation_text="점심", annotation_position="top left", annotation_font_color="#5B6474")
    fig.update_xaxes(range=[x0, x1], tickformat="%H:%M", dtick=3_600_000, showgrid=True, gridcolor="#EEF0F3")
    fig.update_yaxes(visible=False)
    fig.update_layout(height=190, margin=dict(l=10, r=10, t=30, b=10), plot_bgcolor="#FFFFFF",
                      legend=dict(orientation="h", y=1.25, x=0, title_text=""), bargap=0.3)
    return fig


def picked_from_chart(event, fig: go.Figure) -> list[str]:
    ids = []
    for p in (event or {}).get("selection", {}).get("points", []):
        cd = p.get("customdata")
        if cd is None and p.get("curve_number") is not None:
            cd = fig.data[p["curve_number"]].customdata[p.get("point_index", p.get("point_number", 0))]
        if cd is not None:
            ids.append(cd[0])
    return ids


st.markdown("#### 오늘 시간표")
tl = ws.today_timeline(DAY, team)
fig = timeline_figure(tl) if not tl.empty else None
if fig is None or not len(fig.data):
    st.info("오늘 배정된 작업이 없습니다.")
else:
    event = st.plotly_chart(fig, width="stretch", config=CHART_CONFIG, on_select="rerun", selection_mode="points",
                            key="dash_timeline")
    picked = picked_from_chart(event, fig)
    if picked and tuple(picked) != ss.get("tl_last_pick"):  # 칸을 새로 누르면 ② 목록에서 그 작업을 펼친다
        ss.tl_last_pick, ss.cal_date, ss.open_wo = tuple(picked), DAY, picked[0]
    st.caption("칸을 누르면 아래 목록에서 그 작업이 펼쳐집니다.")


# ---------------------------------------------------------------- ② 달력 + 선택 날짜 작업 목록
CAL_CSS = f"""
.fc .fc-daygrid-day.fc-day-today {{ background: #F3F6FA; box-shadow: inset 0 0 0 2px {NAVY}; }}
.fc .fc-event, .fc .fc-event-title, .fc .fc-daygrid-more-link {{ font-size: 12px; }}
.fc .fc-col-header-cell-cushion, .fc .fc-toolbar-title {{ font-weight: 700; }}
.fc .fc-toolbar-title {{ font-size: 1.1rem; color: {NAVY}; }}
.fc .ev-urgent-done {{ opacity: 0.85; }}
"""


def handle_calendar(value: dict | None) -> None:
    """dateClick → 그 날짜, eventClick → 그 이벤트 날짜(+ 작업이면 펼침). 같은 값은 한 번만 처리한다
    (부품은 마지막 콜백 값을 계속 돌려준다)."""
    if not value or not value.get("callback"):
        return
    sig = json.dumps(value, sort_keys=True, default=str)
    if sig == ss.get("cal_last_value"):
        return
    ss.cal_last_value = sig
    if value["callback"] == "dateClick":
        ss.cal_date = ws.clicked_date(value["dateClick"].get("date")) or ss.cal_date
    elif value["callback"] == "eventClick":
        ev = value["eventClick"].get("event", {})
        ss.cal_date = ws.clicked_date(ev.get("start")) or ss.cal_date
        if wo_id := (ev.get("extendedProps") or {}).get("wo_id"):
            ss.open_wo = wo_id


st.markdown("#### 달력")
start, end = ws.calendar_range(DAY, team)
# 달 이동(datesSet)은 이 부품이 알려 주지 않아 앞뒤 몇 달의 이벤트를 미리 넣는다 (ws.calendar_range).
# key에 기준 날짜를 넣어 날짜가 바뀌면 달력이 새 initialDate로 다시 열리게 한다.
handle_calendar(calendar(events=ws.calendar_events(DAY, team, start, end), options=ws.calendar_options(DAY),
                         custom_css=CAL_CSS, callbacks=["dateClick", "eventClick"], key=f"worker_cal_{team}_{DAY}"))
st.caption("주황 = 1~2일 지남 · 빨강 = 3일 이상 지났거나 긴급 · 진한 빨강 = 완료한 긴급 작업 · "
           "남색 = 오늘·앞으로 할 작업 · 회색 = 완료")


def order_label(w: dict, late: int) -> str:
    tags = (" · 긴급" if w.get("urgent") else "") + (f" · {late}일 지남" if late and w["status"] != "DONE" else "")
    return f"{ws.order_title(w)} · {w['start'] or '-'} · {app_db.WO_STATUS_KO[w['status']]}{tags}"


def trend_chart(w: dict) -> None:
    sensor = ws.KEY_SENSOR.get(w["comp"], "vibration")
    try:
        trend = wp.cached_trend(w["machine_id"], sensor, AS_OF)
        ui.sensor_chart({"sensor": sensor, "base": SENSOR_MEAN[sensor]}, trend, datetime.fromisoformat(AS_OF))
    except Exception as e:
        st.caption(f"센서 차트를 그리지 못했습니다: {e}")


def order_item(w: dict, late: int) -> None:
    """접힌 줄 + 펼친 내용(작업 종류·시간, 점검 항목, 주의 사항, 근거 ID, 시작/완료 보고). 센서 그래프는 버튼으로만."""
    wid, urgent = w["wo_id"], bool(w.get("urgent"))
    with st.expander(order_label(w, late), expanded=ss.get("open_wo") == wid):
        tags = [ui.badge_html(app_db.WO_STATUS_KO[w["status"]], wp.STATUS_KIND[w["status"]])]
        if urgent:
            tags.append(ui.badge_html("긴급", "red"))
        if late and w["status"] != "DONE":
            tags.append(ui.badge_html(f"{late}일 지남", "red" if late >= 3 else "orange"))
        st.markdown(" ".join(tags), unsafe_allow_html=True)
        st.markdown(f"**{TASK_LABEL.get(w['task_type'], '작업')}** · {w['as_of_date']} {w['start'] or '-'}~{w['end'] or '-'}"
                    f" · 작업 지시 {wid} · 근거 {w.get('evidence_id') or '-'}")
        if w.get("checklist"):
            st.markdown("**점검 항목**\n" + "\n".join(f"- {c}" for c in w["checklist"]))
        st.markdown("**주의 사항**\n" + "\n".join(f"- {c}" for c in wp.caution_lines(w, {})))

        if w["status"] == "ASSIGNED" and urgent:
            st.caption("화면 맨 위 [출동 확인] 상자에서 출동을 확인하세요.")
        elif w["status"] == "ASSIGNED":
            if st.button("작업 시작", key=f"dash_start_{wid}", type="primary"):
                try:
                    services.start_work(wid, user)
                    ss.open_wo = wid
                    st.rerun()
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
        elif w["status"] == "IN_PROGRESS":
            if ss.get("report_wo") == wid:
                wp.report_form(w, prefix="dash_")
            elif st.button("완료 보고", key=f"dash_report_{wid}", type="primary"):
                ss.report_wo, ss.open_wo = wid, wid
                st.rerun()
        else:  # 완료: 완료 시각 + 완료 보고 요약 (달력의 "긴급 완료"를 눌러 펼쳤을 때도)
            st.caption(f"완료 {(w['done_at'] or '').replace('T', ' ')}")
            if rep := reports.get(wid):
                st.markdown(f"**완료 보고** · {app_db.feedback_sentence(rep)}")

        showing = ss.get("trend_wo") == wid
        if st.button("센서 추이 닫기" if showing else "센서 추이 보기", key=f"dash_trend_{wid}"):
            ss.trend_wo, ss.open_wo = (None if showing else wid), wid
            st.rerun()
        if showing:
            trend_chart(w)


cal_date = ss.cal_date
picked_orders = ws.orders_on(cal_date, team)
reports = {r["wo_id"]: r for r in app_db.work_reports(team=team, as_of_date=cal_date)}
late = ws.overdue_days(DAY, cal_date) if any(w["status"] != "DONE" for w in picked_orders) else 0
st.markdown(f"**{date.fromisoformat(cal_date):%m-%d}" + (f" ({late}일 지남)" if late else "")
            + f" · {len(picked_orders)}건**")
if not any(w["status"] != "DONE" for w in picked_orders):
    st.caption("이 날짜에 남은 작업이 없습니다.")
for w in picked_orders:
    order_item(w, late)


# ---------------------------------------------------------------- ③ 내 설비 실시간 센서 (2초마다 이 구역만)
def sensor_cell(row: pd.Series, sensor: str) -> str:
    v = row[sensor]
    if v is None or pd.isna(v):
        return "-"
    return f"{v:.1f} ({row['key_pct']:+.0f}%)" if sensor == row["key_sensor"] else f"{v:.1f}"


@st.fragment(run_every=2)
def live_sensor_section() -> None:
    df = ws.live_sensor_table(DAY, team, AS_OF)  # 캐시하지 않는다 (완료 보고하면 다음 갱신 때 빠진다)
    if df.empty:
        st.info("지금 맡은 설비가 없습니다.")
        return
    view = pd.DataFrame({
        "설비": df["machine_id"], "내 작업": df["task"],
        **{SENSOR_KO[s]: [sensor_cell(r, s) for _, r in df.iterrows()] for s in SENSOR_KO},
        "추이": df["trend"],
        "평소 대비": [f"{SENSOR_KO[k]} {p:+.0f}%" if pd.notna(p) else "-" for k, p in zip(df["key_sensor"], df["key_pct"])],
        "데이터": df["source"], "상태": df["state"]})
    styled = view.style.apply(lambda r: [f"color: {RED}; font-weight: 600" if r["상태"] == "경고선 초과" else ""] * len(r),
                              axis=1)
    st.dataframe(styled, hide_index=True, width="stretch", column_config={
        "설비": st.column_config.NumberColumn(format="#%d"),
        "추이": st.column_config.LineChartColumn("추이", help="작업 부품의 짝 센서 (실시간 최근 60초 / 시간 단위 최근 24시간)")})
    st.caption("실시간 = 1초 스트림(관리자 실시간 감지 대상 설비), 시간 단위 = 기준 시각의 마지막 기록")


st.markdown("#### 내 설비 실시간 센서")
live_sensor_section()


# ---------------------------------------------------------------- ④ ⑤ 점검 결과 분포 · 조치 분포
def hbar(df: pd.DataFrame, colors: list[str]) -> go.Figure:
    fig = go.Figure(go.Bar(x=df["count"], y=df["label"], orientation="h", marker_color=colors,
                           text=[f"{c}건 · {p:.0f}%" for c, p in zip(df["count"], df["pct"])],
                           textposition="outside", cliponaxis=False, hovertemplate="%{y}: %{text}<extra></extra>"))
    fig.update_yaxes(autorange="reversed", categoryorder="array", categoryarray=list(df["label"]))
    fig.update_xaxes(visible=False, range=[0, max(int(df["count"].max()), 1) * 1.45])
    fig.update_layout(height=230, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="#FFFFFF")
    return fig


period, scope = wp.stats_filters("dash")
cause_df = wp.cached_stat("cause_distribution", DAY, team, scope, period)
action_df = wp.cached_stat("action_distribution", DAY, team, scope, period)
left, right = st.columns(2)
with left:
    st.markdown("#### 점검 결과 분포")
    if not int(cause_df["count"].sum()):
        st.info(ws.NO_REPORTS)
    else:
        st.plotly_chart(hbar(cause_df, [CAUSE_COLOR[c] for c in cause_df["label"]]), width="stretch",
                        config=CHART_CONFIG, key="dash_cause")
        st.caption(ws.cause_summary_text(cause_df, period))
with right:
    st.markdown("#### 조치 분포")
    if not int(action_df["count"].sum()):
        st.info(ws.NO_REPORTS)
    else:
        top_i = action_df["count"].idxmax()
        st.plotly_chart(hbar(action_df, [NAVY if i == top_i else NAVY_LIGHT for i in action_df.index]),
                        width="stretch", config=CHART_CONFIG, key="dash_action")
        st.caption(ws.action_summary_text(action_df))
