"""작업자 화면 공통 (오늘 작업·대시보드·작업 이력이 같이 쓴다): 기준 날짜, 긴급 [출동 확인] 상자, 작업 카드,
완료 보고 양식, 조사 내용, 집계 필터·캐시.

같은 작업이 다른 화면 구역에서 다시 그려질 수 있어 작업 카드·완료 보고 위젯 key 앞에 prefix를 붙인다.
"""
from __future__ import annotations

from datetime import date, datetime

import streamlit as st

import services
from config import COMP_LABEL_KO
from store import app_db
from tools.plan_tools import COMPS, TASK_LABEL
from ui import components as ui

ss = st.session_state
STATUS_KIND = {"ASSIGNED": "gray", "IN_PROGRESS": "orange", "DONE": "navy"}
DEFAULT_ACTION = {"replace": "부품 교체", "temporary_check": "임시 조치", "sensor_check": "센서 재고정"}


def _user() -> dict:
    return ss.user


def shared_day(team: str) -> tuple[date, str]:
    """관리자가 정한 기준 날짜 (없으면 우리 팀 작업이 있는 마지막 날짜, 그것도 없으면 2015-10-01)."""
    cur = app_db.current_day()
    if cur:
        who = f"관리자 {cur['updated_by']}" if cur.get("updated_by") else "관리자"
        return date.fromisoformat(cur["day"]), f"{who} 설정 · {cur['updated_at'].replace('T', ' ')}"
    days = app_db.work_order_dates(team)
    return (date.fromisoformat(days[-1]) if days else date(2015, 10, 1)), "관리자가 아직 날짜를 정하지 않음"


def day_sidebar(team: str) -> date:
    """기준 날짜: 관리자가 오늘의 분석에서 고른 날짜를 따른다 (작업자는 바꾸지 않음).
    알림 패널이 10초마다 공유 날짜를 확인해서, 바뀌면 페이지를 다시 그린다 (ss.seen_day)."""
    day, note = shared_day(team)
    ss.seen_day = f"{day:%Y-%m-%d}"
    with st.sidebar:
        st.subheader("기준 날짜")
        st.markdown(f"**{day:%Y-%m-%d}**")
        st.caption(note)
    return day


def show_flash() -> None:
    if msg := ss.pop("flash", None):
        st.success(msg)


@st.cache_data(show_spinner=False)
def _cached_trend(machine_id: int, sensor: str, as_of: str, live_version: str):
    return services.sensor_trend(machine_id, sensor, 72, as_of)


def cached_trend(machine_id: int, sensor: str, as_of: str):
    return _cached_trend(machine_id, sensor, as_of, services.live_version())  # 입력 데이터가 바뀌면 다시 읽음


def caution_lines(wo: dict, brief: dict) -> list[str]:
    lines = ["작업 전 설비 정지·잠금 절차를 확인하세요."]
    if wo["task_type"] == "temporary_check":
        lines.append("부품 재고가 없습니다. 교체하지 말고 점검·임시 조치만 하고, 발주 입고 후 교체 일정을 다시 받습니다.")
    if brief.get("label") == "센서·설치 이상 가능성":
        lines.append("최근 교체한 부품입니다. 재교체 전에 센서 고정·배선 상태를 먼저 확인하세요.")
    return lines


def _minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def report_form(wo: dict, prefix: str = "") -> None:
    """완료 보고 양식: 원인·부품·조치·시간·부품 수량 + 지시와 달랐던 점 + 상세 메모(필수, 측정값도 메모에).
    [메모 정리 도움]은 메모를 4개 항목 형식 초안으로 바꿔 메모 칸에 넣는다 (작업자가 확인·수정 후 제출)."""
    wid = wo["wo_id"]
    k = f"{prefix}{{}}_{wid}".format
    memo_key = k("memo")
    if (draft := ss.pop(k("memo_draft"), None)) is not None:  # 정리 초안은 위젯을 만들기 전에 넣는다
        ss[memo_key] = draft
        st.info("메모 정리 초안을 메모 칸에 넣었습니다. 사실과 다른 부분이 없는지 확인·수정한 뒤 제출하세요.")
    with st.form(k("report_form")):
        st.markdown("**완료 보고**")
        c1, c2, c3 = st.columns(3)
        cause = c1.selectbox("실제 원인 유형", app_db.CAUSE_TYPES, key=k("cause"))
        comp = c2.selectbox("실제 고장 부품", COMPS, index=COMPS.index(wo["comp"]) if wo["comp"] in COMPS else 0,
                            format_func=lambda c: f"{COMP_LABEL_KO[c]} ({c})", key=k("comp"))
        action = c3.selectbox("조치 내용", app_db.ACTIONS,
                              index=app_db.ACTIONS.index(DEFAULT_ACTION.get(wo["task_type"], "부품 교체")),
                              key=k("action"))
        c4, c5 = st.columns(2)
        planned = _minutes(wo["end"]) - _minutes(wo["start"]) if wo["start"] and wo["end"] else 60
        duration = c4.number_input("실제 소요 시간(분)", 1, 1440, planned, step=5, key=k("dur"))
        parts = c5.number_input("사용 부품 수량", 0, 20, 1 if wo["task_type"] == "replace" else 0, key=k("parts"))
        deviation = st.text_input("지시와 달랐던 점 (선택)", key=k("dev"),
                                  placeholder="예: 지시는 교체였지만 윤활 보충으로 해결")
        memo = st.text_area(f"상세 메모 (필수, {services.MIN_MEMO_CHARS}자 이상)", key=memo_key, height=150,
                            placeholder=services.MEMO_GUIDE)
        b1, b2 = st.columns([1, 1])
        organize = b1.form_submit_button("메모 정리 도움", help="짧게 쓴 메모를 4개 항목 형식으로 정리한 초안을 만듭니다."
                                                             " 쓰지 않은 사실은 추가하지 않습니다.")
        submitted = b2.form_submit_button("완료 보고 제출", type="primary")

    if organize:
        try:
            with st.spinner("메모 정리 중"):
                ss[k("memo_draft")] = services.organize_memo(memo)
            st.rerun()
        except Exception as e:
            st.error(f"메모 정리 실패: {e}")
    if submitted:
        if len(memo.strip()) < services.MIN_MEMO_CHARS:
            st.error(f"상세 메모를 {services.MIN_MEMO_CHARS}자 이상 적어 주세요 (지금 {len(memo.strip())}자)."
                     " 발견한 상태와 측정값, 원인 판단 근거, 조치 후 상태, 다음 점검 주의점을 적으면 다음 조사에 쓰입니다."
                     " [메모 정리 도움]을 눌러도 됩니다.")
            return
        try:
            out = services.complete_work(wid, _user(), cause, comp, action, int(duration), int(parts), memo, deviation)
            ss.report_wo = None
            note = "" if out["indexed"] else " (현장 기록 색인은 실패해 다음에 다시 시도해야 합니다)"
            ss.flash = f"완료 보고를 저장했습니다.{note} {out['sentence']}"
            st.rerun()
        except (ValueError, PermissionError) as e:
            st.error(str(e))


def finding_section(wo: dict, f: dict | None, expanded: bool) -> None:
    """관리자 설비별 조사 탭과 같은 내용: 3줄(이상 / 1순위 원인 / 할 일) + 자세히 보기(센서 추이·주의 사항 포함)."""
    if f is None:
        st.caption("이 작업의 조사 기록이 없습니다.")
        return
    ui.finding_lines(f)
    with st.expander("자세히 보기", expanded=expanded):
        st.markdown("**주의 사항**")
        st.markdown("\n".join(f"- {c}" for c in caution_lines(wo, f)))
        ui.finding_details(f)
        st.markdown("**센서 추이**")
        try:
            ui.sensor_chart(f, cached_trend(f["machine_id"], f["sensor"], f"{wo['as_of_date']} 06:00:00"),
                            datetime.fromisoformat(f"{wo['as_of_date']}T06:00:00"))
        except Exception as e:
            st.caption(f"센서 차트를 그리지 못했습니다: {e}")


def order_card(wo: dict, show_date: bool = False, prefix: str = "") -> None:
    """작업 카드: 시간·설비·작업·상태 + 조사 내용 + 작업 시작·완료 보고 버튼.
    작업 시작 전(배정)에는 조사 3줄을 바로 보여 주고, 시작 후에는 완료 보고 양식이 잘 보이도록 접어 둔다."""
    is_focus = ss.get("focus_wo") == wo["wo_id"]
    urgent = bool(wo.get("urgent"))
    with st.container(border=True):
        when = f"{wo['as_of_date']} " if show_date else ""
        head = (f"{when}{wo['start'] or '-'}~{wo['end'] or '-'} · #{wo['machine_id']}"
                f" {COMP_LABEL_KO.get(wo['comp'], wo['comp'])} · {TASK_LABEL.get(wo['task_type'], '작업')}")
        tag = f"{ui.badge_html('긴급', 'red')} &nbsp;" if urgent else ""
        st.markdown(f"{tag}**{head}** &nbsp; {ui.badge_html(app_db.WO_STATUS_KO[wo['status']], STATUS_KIND[wo['status']])}",
                    unsafe_allow_html=True)
        if urgent and wo["status"] == "ASSIGNED":
            st.markdown(f'<span class="urgent-time">출동 {wo["start"] or "-"}</span>', unsafe_allow_html=True)
        st.caption(f"작업 지시 {wo['wo_id']} · 승인 {wo['approved_by'] or '-'}")
        f = services.finding_view(wo["as_of_date"], wo["machine_id"])
        if wo["status"] == "ASSIGNED":
            finding_section(wo, f, expanded=is_focus)
        else:
            with st.expander("조사 내용 다시 보기", expanded=False):
                if f is None:
                    st.caption("이 작업의 조사 기록이 없습니다.")
                else:
                    ui.finding_lines(f)
                    st.markdown("**주의 사항**")
                    st.markdown("\n".join(f"- {c}" for c in caution_lines(wo, f)))
                    ui.finding_details(f)

        if wo["status"] == "ASSIGNED" and urgent:
            st.caption("화면 맨 위 [출동 확인] 상자에서 출동을 확인하세요.")
        elif wo["status"] == "ASSIGNED":
            if st.button("작업 시작", key=f"{prefix}start_{wo['wo_id']}", type="primary"):
                try:
                    services.start_work(wo["wo_id"], _user())
                    st.rerun()
                except (ValueError, PermissionError) as e:
                    st.error(str(e))
        elif wo["status"] == "IN_PROGRESS":
            if ss.get("report_wo") == wo["wo_id"]:
                report_form(wo, prefix)
            elif st.button("완료 보고", key=f"{prefix}report_{wo['wo_id']}", type="primary"):
                ss.report_wo = wo["wo_id"]
                st.rerun()
        else:
            st.caption(f"완료 {(wo['done_at'] or '').replace('T', ' ')}")


def urgent_box(waiting: list[dict]) -> None:
    """긴급 점검 [출동 확인] 상자: 아직 출동 확인 전인 긴급 작업을 화면 맨 위에 눈에 띄게.
    새 긴급 알림은 st.toast로 한 번 더 알린다 (알림 패널이 10초마다 확인해 페이지를 다시 그림)."""
    user = _user()
    toasted = ss.setdefault("toasted_urgent", set())
    for n in app_db.notifications_for(user, 20):
        if ui.is_urgent_notice(n) and not n["read_at"] and n["id"] not in toasted:
            toasted.add(n["id"])
            st.toast(n["title"])
    for wo in waiting:
        with st.container(border=True, key=f"urgent_box_{wo['wo_id']}"):
            ev = f" (근거 {wo['evidence_id']})" if wo.get("evidence_id") else ""
            st.markdown(f'<div class="alert-banner">{TASK_LABEL.get(wo["task_type"], "긴급 점검")}: #{wo["machine_id"]}'
                        f' {COMP_LABEL_KO.get(wo["comp"], wo["comp"])} — {wo["start"] or "-"} 출동{ev}</div>',
                        unsafe_allow_html=True)
            st.caption(f"작업 지시 {wo['wo_id']} · {wo['as_of_date']} {wo['start']}~{wo['end']} · 자동 배정(관리자 승인 없음)")
            if st.button("출동 확인", key=f"dispatch_{wo['wo_id']}", type="primary"):
                try:
                    services.start_work(wo["wo_id"], user)
                    for n in app_db.notifications_for(user, 50):  # 이 작업 알림은 읽음 처리
                        if n["link_wo_id"] == wo["wo_id"] and not n["read_at"]:
                            app_db.mark_read(n["id"], user)
                    ss.flash = f"출동 확인했습니다. #{wo['machine_id']} 점검을 마치면 완료 보고를 남겨 주세요."
                    st.rerun()
                except (ValueError, PermissionError) as e:
                    st.error(str(e))


PERIODS = {"최근 30일": 30, "전체": None}
SCOPES = {"우리 팀": "team", "전체 팀": "all"}


def stats_filters(key_prefix: str) -> tuple[int | None, str]:
    """기간·범위 필터 한 줄 -> (period, scope). 아래 그래프·표에 같이 적용한다."""
    f1, f2 = st.columns(2)
    p = f1.segmented_control("기간", list(PERIODS), default="최근 30일", required=True, key=f"{key_prefix}_period")
    s = f2.segmented_control("범위", list(SCOPES), default="우리 팀", required=True, key=f"{key_prefix}_scope")
    return PERIODS[p or "최근 30일"], SCOPES[s or "우리 팀"]


@st.cache_data(ttl=10, show_spinner=False)
def _cached(name: str, db: str, day: str, team: str, scope: str, period: int | None, n_reports: int, n_orders: int):
    from store import worker_stats as ws

    return getattr(ws, name)(day, team, scope, period)


def cached_stat(name: str, day: str, team: str, scope: str, period: int | None):
    """worker_stats 집계(cause_distribution / action_distribution / top_machines)를 10초 캐시.
    키에 DB 경로·완료 보고 수·작업 지시 수를 넣어 새 보고·새 배정이 바로 반영되게 한다."""
    from store import worker_stats as ws

    return _cached(name, str(app_db.DB_PATH), day, team, scope, period, ws.report_count(), ws.order_count())


def waiting_urgent(day: str, team: str) -> tuple[list[dict], list[dict], list[dict]]:
    """(오늘 작업, 이전 날짜 미완료, 출동 확인 대기 긴급 작업). 긴급 작업을 맨 위로, 그다음 시작 시각 순."""
    orders = sorted(app_db.work_orders(day, team), key=lambda w: (-int(w.get("urgent") or 0), w["start"] or ""))
    carried = app_db.open_orders_before(day, team)
    return orders, carried, [w for w in orders + carried if w.get("urgent") and w["status"] == "ASSIGNED"]
