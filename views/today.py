"""오늘의 분석: 사이드바(기준 시각·오늘 지표) + 탭(대시보드 / 설비별 조사 / 승인) + 채팅 아이콘(대화창) + 개발자 로그.
대시보드의 "실시간 감지" 카드에 실시간 모니터(ui/live_monitor.py)가 들어 있다 ([열기] 토글을 켤 때만 그림)."""
from __future__ import annotations

import html
import json
import re
import time
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from ops_settings import load_settings
from ui import components as ui
from ui import live_monitor as lm
from ui import mock
from ui import state as S

ss = st.session_state
MOCK = S.use_mock()
CFG = load_settings()
MAX_TOOL_CALLS = mock.MAX_TOOL_CALLS
STAGES = [("predict_failure", "예측"), ("investigate_machines", "조사"), ("ask_planner_agent", "계획"),
          ("승인 대기", "승인 요청")]
STAGE_KIND = {"완료": "navy", "진행 중": "orange", "대기": "gray"}
CHANGED_STYLE = "background-color: #FCF1E4; font-weight: 600;"  # 재계획으로 배정이 바뀐 행
BLOCKED_ROUTE = "입력 가드에서 차단 — 에이전트 호출 없음"

S.init()
if "as_of_date" not in ss:  # 다른 페이지에 다녀오면 위젯 값이 지워지므로 기준 날짜로 다시 채운다
    ss.as_of_date = ss.day

# ──────────────────────────────────────────────────────────────
# 사이드바: 기준 시각 + 오늘 지표
# ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.subheader("기준 시각")
    st.date_input("기준 날짜", min_value=S.TEST_START, max_value=S.TEST_END, key="as_of_date",
                  on_change=S.on_date_change, disabled=S.job_running(),
                  help="테스트 구간(10~12월)만 선택할 수 있습니다. 모든 조회는 이 시각 이후 데이터를 볼 수 없습니다.")
    st.caption(f"현재 시각으로 간주: {S.as_of():%Y-%m-%d %H:%M}")
    st.caption("7의 배수 날짜(7·14·21·28일)는 고위험 0대 예시입니다." if MOCK else f"데모 날짜: {mock.DEMO_DATES}")
    st.button("다음 날로", width="stretch", key="next_day_btn", on_click=S.next_day,
              disabled=ss.day >= S.TEST_END or S.job_running(),
              help="기준 시각을 하루 넘기고, 어제 예측을 실제 고장과 비교합니다.")

    st.divider()
    st.subheader("오늘 지표")
    res = ss.result
    c1, c2 = st.columns(2)
    c1.metric("고위험", "-" if res is None else len(res["findings"]))
    c2.metric("사람 확인", "-" if res is None else sum(f["label"] != "예측 고장" for f in res["findings"]))
    c1.metric("승인 대기", "-" if res is None else len(S.pending_requests()))
    if ss.prev:
        try:
            vr = S.verify_rows(ss.prev)
            n_high = int(vr["예측"].ne("-").sum())
            n_hit = int((vr["판정"] == "적중").sum())
            c2.metric("어제 예측 적중", f"{n_hit}/{n_high}" if n_high else "고위험 없음")
        except Exception as e:
            st.error(f"어제 검증 실패: {e}")
    else:
        c2.metric("어제 예측 적중", "-")
    if MOCK:
        st.caption("목업 모드: 모든 수치는 가짜 데이터입니다.")

# ──────────────────────────────────────────────────────────────
# 본문
# ──────────────────────────────────────────────────────────────
st.subheader(f"오늘의 분석 · {S.as_of():%Y-%m-%d %H:%M}" + (" (목업)" if MOCK else ""))
tab1, tab2, tab3 = st.tabs(["대시보드", "설비별 조사", "승인"], key="today_tab", on_change="rerun")


def render_job(job: dict) -> None:
    """(실제) 진행 단계와 설비별 조사 진행 막대."""
    seen = {e["msg"].split("`")[1] for e in job["events"] if "` 시작" in e["msg"]}
    if job["stage"] == "승인 대기":
        seen.add("승인 대기")
    current = job["stage"]
    line = []
    for key, text in STAGES:
        mark = "진행 중" if key == current else ("완료" if key in seen else "대기")
        line.append(f"{text} {ui.badge_html(mark, STAGE_KIND[mark])}")
    st.markdown(" &nbsp;→&nbsp; ".join(line), unsafe_allow_html=True)
    prog = job.get("progress") or {}
    if prog:
        cols = st.columns(min(len(prog), 6))
        for i, (m, p) in enumerate(sorted(prog.items())):
            k = min(p["step"], MAX_TOOL_CALLS)
            label = f"#{m} 조회 {k}회" + (" 완료" if p["done"] and not p["error"] else " 오류" if p["error"] else "")
            cols[i % len(cols)].progress(1.0 if p["done"] else k / MAX_TOOL_CALLS, text=label)


def finish_job(job: dict) -> None:
    """(실제) 백그라운드 점검이 끝나면 결과 파일을 읽어 화면 상태를 갱신."""
    ss.job_thread = None
    ss.trace = S.trace_from_events(job["events"])
    if job["error"]:
        ss.error = f"점검 중 오류: {job['error']}"
    result = job["result"] or {}
    ss.pending = result.get("requests") if result.get("status") == "pending" else None
    ss.last_answer = result.get("answer")
    ss.result = S.svc().load_day(S.as_of_text())


def run_mock_check() -> None:
    ss.trace = []
    t = S.as_of()
    with st.status("오늘 점검 처리 중", expanded=True) as status:
        S.log("Supervisor", "버튼 메시지 수신 → 예측 → 조사 → 계획 수립")
        st.write("예측: 설비 100대 고장 위험 계산 중")
        time.sleep(0.6)
        pred = mock.mock_predict(t)
        high = pred[pred.high_risk].sort_values("prob", ascending=False)
        S.log("ML", f"100대 예측 완료 → 고위험 {len(high)}대 (임계값 {mock.THRESHOLD})")
        st.write(f"고위험 {len(high)}대: {', '.join(f'#{m}' for m in high.machine_id) or '없음'}")

        findings = []
        if len(high) == 0:
            st.write("조사: 고위험 설비가 없어 조사를 건너뜁니다.")
            S.log("Investigator", "K=0 → 생성 없음")
        else:
            st.write(f"조사: 설비 {len(high)}대를 동시에 조사합니다.")
            cols = st.columns(len(high))
            bars = [c.progress(0, text=f"#{m}") for c, m in zip(cols, high.machine_id)]
            results = [mock.mock_investigate(r, t) for _, r in high.iterrows()]
            steps = [len(x["tool_calls"]) for x in results]
            for s in range(1, MAX_TOOL_CALLS + 1):
                time.sleep(0.25)
                for b, m, n in zip(bars, high.machine_id, steps):
                    k = min(s, n)
                    b.progress(k / n, text=f"#{m} 조회 {k}/{n}" + (" 완료" if k == n else ""))
            findings = results
            for f in findings:
                S.log("Investigator", f"#{f['machine_id']} 조사 완료 ({len(f['tool_calls'])}회) → {f['label']}, {f['evidence_id']}")

        st.write("계획: 재고·중요도·팀을 반영해 배정 중")
        time.sleep(0.5)
        plan, drafts = mock.mock_plan(findings, t)
        S.log("Planner", f"배정 {int(plan['team'].notna().sum()) if len(plan) else 0}건, 초안 {len(drafts)}건 (등록 안 함)")
        if drafts:
            S.log("Supervisor", f"submit_* 호출 → 승인 대기 {len(drafts)}건")
        status.update(label="점검 완료 · 승인 대기 중" if drafts else "점검 완료", state="complete", expanded=False)
    ss.result = {"pred": pred, "findings": findings, "plan": plan, "drafts": drafts, "plan_version": 1,
                 "utilization": {}, "tomorrow": [], "team_windows": {}, "diff": pd.DataFrame()}
    st.rerun()


@st.fragment(run_every=60)
def schedule_chart(plan: pd.DataFrame) -> None:
    """팀별 하루 일정 간트 (+ 그날 긴급 작업) + "지금" 세로선 (근무 시간 밖이면 없음). 1분마다 다시 그린다."""
    import live_ops

    try:
        urgent = S.live().urgent_orders(f"{ss.day:%Y-%m-%d}")
    except Exception:
        urgent = []
    fig = ui.schedule_figure(plan, ss.day, CFG, urgent=urgent)
    if fig is not None:
        ui.add_now_line(fig, ss.day, CFG, live_ops.NOW_HM(CFG))
        st.plotly_chart(fig, width="stretch", key="schedule_chart")


# ── 대시보드 ──────────────────────────────────────────────
with tab1:
    with st.container(border=True):
        top = st.columns([3, 1])
        top[0].markdown(f"**기준 시각 {S.as_of():%Y-%m-%d %H:%M}** · 정비 인력 {mock.CREWS}팀 · "
                        f"근무 {CFG['work_start']}~{CFG['work_end']}")
        start = top[1].button("오늘 점검 시작", type="primary", width="stretch", key="start_btn",
                              disabled=ss.result is not None or S.job_running())
        if ss.error:
            st.error(ss.error)

        if start and MOCK:
            run_mock_check()
        if start and not MOCK:
            try:
                S.svc().get_supervisor()
                ss.error = None
                S.svc().start_daily_check(S.as_of_text(), ss.thread_id)
                ss.job_thread = ss.thread_id
            except Exception as e:
                ss.error = f"점검 시작 실패: {type(e).__name__}: {e}"
            st.rerun()

        if S.job_running():
            with st.status("오늘 점검 처리 중", expanded=True):
                box = st.empty()
                while True:  # 0.5초마다 백그라운드 작업 상태를 읽어 진행 표시
                    job = S.svc().get_job(ss.job_thread)
                    with box.container():
                        render_job(job)
                    if not job["running"]:
                        break
                    time.sleep(0.5)
            try:
                finish_job(job)
            except Exception as e:
                ss.error = f"결과 읽기 실패: {type(e).__name__}: {e}"
            st.rerun()

        res = ss.result
        if res is None:
            st.info("아직 오늘 점검을 시작하지 않았습니다. [오늘 점검 시작]을 누르세요.")
        else:
            n_pending = len(S.pending_requests())
            if n_pending:
                st.warning(f"승인 대기 {n_pending}건 — 승인 탭에서 처리하세요.")
            elif ss.last_answer and not MOCK:
                st.success(ss.last_answer)

    if not MOCK:  # 재고 0 부품: 조치가 필요한 것만, [확인]하면 같은 상태인 동안 숨김 (발주 진행 중은 한 줄 요약)
        try:
            stock = S.svc().stock_card(S.as_of_text())
        except Exception as e:
            stock = {"show": [], "in_progress": 0}
            st.caption(f"재고 상태를 읽지 못했습니다: {e}")
        show, n_progress = stock["show"], stock["in_progress"]
        if show:
            with st.container(border=True, key="stock_card"):
                st.markdown("**재고 0 부품**")
                for a in show:
                    left, right = st.columns([8, 1], vertical_alignment="center")
                    with left:
                        getattr(st, a["level"])(f"{a['part_name']} ({a['comp']}): {a['status']}")
                    if right.button("확인", key=f"stock_ack_{a['comp']}", type="tertiary",
                                    help="확인한 상태는 다시 띄우지 않습니다. 상태가 바뀌면 다시 보입니다."):
                        S.svc().ack_stock_alert(a["comp"], a["key"], by=S.user_id())
                        st.rerun()
                if n_progress:
                    st.caption(f"발주 진행 중 {n_progress}건 (작업 현황에서 확인)")
        elif n_progress:
            st.caption(f"재고 0 부품 발주 진행 중 {n_progress}건 (작업 현황에서 확인)")

    res = ss.result
    if res is not None:
        plan = res["plan"]
        version = res.get("plan_version", 1)
        with st.container(border=True):  # 배정안(점수·확률·중요도)도 이 카드의 표에 함께 보인다
            st.subheader("팀별 하루 일정" + (f" · 배정안 v{version}" if version > 1 else ""))
            if version > 1 and res.get("plan_reason"):
                st.caption(f"재계획 사유: \"{res['plan_reason']}\" — {res.get('plan_note') or ''}")
            if plan.empty:  # 긴급 작업 간트는 아래 실시간 감지 카드 다음에 따로 나온다
                st.success("오늘은 24시간 내 고위험 설비가 없습니다. 정기 점검만 진행하세요.")
            else:
                try:
                    has_urgent = bool(S.live().urgent_orders(f"{ss.day:%Y-%m-%d}"))
                except Exception:
                    has_urgent = False
                if not ("team" in plan and plan["team"].notna().any()) and not has_urgent:
                    st.info("오늘 일정에 들어간 작업이 없습니다.")
                else:
                    schedule_chart(plan)
                    ui.schedule_table(plan)
                    st.caption("점수 = 고장 확률 × 중요도 가중치(A 3 · B 2 · C 1) × 재고 계수(재고 0이면 0.8), 센서 점검 × 0.5. "
                               f"점수 높은 작업부터 팀별로 배치합니다 (작업 간 준비 {CFG['prep_min']}분, "
                               f"점심 {CFG['lunch_start']}~{CFG['lunch_end']} 회피, 전문 팀 우선). 계산은 코드가 합니다.")
                util = res.get("utilization") or {}
                tomorrow = res.get("tomorrow") or []
                if util:
                    ui.kpi_cards([(f"{team} 가동률", f"{u:.0%}",
                                   f"작업 시간 / 가용 시간 ({'~'.join(res.get('team_windows', {}).get(team, []))}, 점심 제외)")
                                  for team, u in util.items()] + [("내일 우선", f"{len(tomorrow)}건", None)])
                if tomorrow:
                    t_rows = plan[plan["machine_id"].isin(tomorrow)]
                    st.warning("내일 우선 (근무 종료 전에 끝나지 않음): " + ", ".join(
                        f"#{r.machine_id} {r.comp} {r.task} {r.duration_min}분" for r in t_rows.itertuples()))

    # 실시간 감지: 스트림 -> 즉시 판단 -> 자동 조사 -> 긴급 점검 배정을 이 카드 하나에서 (실시간 모니터 페이지를 옮김).
    # 요약 줄은 5초 fragment로 항상, 경고 표와 모니터(1초 fragment)는 [열기] 토글이 켜졌을 때만 그린다
    # (expander는 접혀 있어도 안의 fragment가 계속 돌기 때문에 토글로 아예 호출하지 않는다).
    def today_alerts() -> list[dict]:
        day = f"{ss.day:%Y-%m-%d}"
        return [a for a in S.live().alert_history(limit=200) if a["as_of"][:10] == day]

    @st.fragment(run_every=5)
    def live_summary() -> None:
        """요약 한 줄: 생성기 상태 · 대상 설비 · 오늘 경고 수(진행 상태별). 토글이 꺼진 채 새 경고가 생기면 "새 경고"."""
        try:
            s = S.live().stream_status()
            rows = today_alerts()
        except Exception as e:
            st.caption(f"실시간 상태를 읽지 못했습니다: {e}")
            return
        ids = {a["alert_id"] for a in rows}
        if ss.get("live_open") or "seen_alert_ids" not in ss:  # 열어 보고 있으면(또는 처음 보면) 모두 본 것으로
            ss.seen_alert_ids = ids
        new = ids - ss.seen_alert_ids
        targets = " ".join(f"#{m}" for m in s["machines"]) if s["state"] != "정지" and s["machines"] else "-"
        if rows:
            by_status = {}
            for a in rows:
                by_status[a["status"]] = by_status.get(a["status"], 0) + 1
            alerts = f"오늘 경고 {len(rows)}건 ({' · '.join(f'{k} {n}' for k, n in by_status.items())})"
        else:
            alerts = "오늘 경고 없음"
        badge = f" &nbsp; {ui.badge_html('새 경고', 'red')}" if new else ""
        st.markdown(f"생성기 {lm.status_badge_html(s)} · 대상 {targets} · {alerts}{badge}", unsafe_allow_html=True)

    @st.fragment(run_every=5)
    def live_alerts_table() -> None:
        """오늘 실시간 경고 표 (경고가 있을 때만)."""
        L = S.live()
        try:
            rows = today_alerts()
        except Exception as e:
            st.caption(f"실시간 경고를 읽지 못했습니다: {e}")
            return
        if not rows:
            return
        st.markdown("**실시간 경고**")
        st.dataframe(pd.DataFrame([{
            "시각": a["created_at"][11:16], "설비": a["machine_id"],
            "부품": f"{ui.COMP_KO.get(a['comp'], a['comp'])}", "확률": a["prob"], "판정": a["level"],
            "근거": f"{a['source']} · {a['message'] or ''}", "진행 상태": a["status"],
            "배정 팀·시간": L.alert_progress(a) if a.get("team") else "-",
        } for a in rows]), hide_index=True, width="stretch", key="live_alert_table",
            column_config={"설비": st.column_config.NumberColumn(format="#%d"),
                           "확률": st.column_config.NumberColumn(format=ui.PROB_FORMAT)})
        st.caption("입력 데이터·실시간 스트림으로 생긴 경고입니다. 긴급 점검은 승인 없이 가장 빨리 비는 팀에 자동 배정되고, "
                   "부품 교체 작업 지시·발주는 기존처럼 승인 후에만 등록됩니다.")

    @st.fragment(run_every=5)
    def urgent_gantt_section() -> None:
        """배정안 간트가 없을 때만 긴급 작업 간트를 따로 (카드 밖)."""
        if ss.result is not None and not ss.result["plan"].empty:
            return
        try:
            urgent = S.live().urgent_orders(f"{ss.day:%Y-%m-%d}")
        except Exception:
            return
        fig = ui.schedule_figure(pd.DataFrame(), ss.day, CFG, urgent=urgent) if urgent else None
        if fig is not None:
            with st.container(border=True):
                st.markdown("**긴급 작업 일정**")
                st.plotly_chart(fig, width="stretch", key="urgent_gantt")

    if "live_open" not in ss:  # 위젯 상태는 다른 페이지에 다녀오면 지워지므로 따로 기억해 둔 값으로 다시 채운다
        ss.live_open = ss.get("live_open_keep", False)
    with st.container(border=True, key="live_card"):
        h1, h2 = st.columns([5, 1], vertical_alignment="center")
        h1.subheader("실시간 감지")
        h2.toggle("열기", key="live_open", help="실시간 경고 표, 생성기 제어, 이상 주입, 설비별 차트를 펼칩니다.")
        ss.live_open_keep = ss.live_open
        live_summary()
        if ss.live_open:
            live_alerts_table()
            lm.render_live_monitor("dash")
    urgent_gantt_section()

    if ss.prev:
        with st.container(border=True):
            pv = ss.prev
            st.subheader(f"어제({pv['as_of']:%m/%d 06:00}) 예측 검증")
            try:
                vr = S.verify_rows(pv)
                if vr.empty:
                    st.success("어제 고위험 예측도, 실제 고장도 없었습니다.")
                else:
                    ui.verify_table(vr)
            except Exception as e:
                st.error(f"검증 실패: {type(e).__name__}: {e}")
            st.caption("실제 고장은 검증 코드만 읽습니다. 에이전트는 기준 시각 이후 데이터를 볼 수 없습니다.")

# ── 설비별 조사 ────────────────────────────────────────────
with tab2:
    res = ss.result
    if res is None or not res["findings"]:
        st.info("조사 결과가 없습니다. 대시보드에서 점검을 먼저 실행하세요. (고위험 0대인 날은 조사도 0건)")
    else:
        with st.container(border=True):  # 여러 설비를 한눈에: 설비별 한 줄 요약
            st.dataframe(pd.DataFrame([{
                "설비": x["machine_id"], "부품": x["comp_name"],
                "분류": "예측 고장" if x["label"] == "예측 고장" else "사람 확인", "할 일": x.get("action_summary") or "-",
            } for x in res["findings"]]), hide_index=True, width="stretch",
                column_config={"설비": st.column_config.NumberColumn(format="#%d", width="small")})

        opts = {f"#{f['machine_id']} · {f['comp_name']} · {f['label']}": f for f in res["findings"]}
        f = opts[st.selectbox("설비 선택", list(opts), key="finding_select")]

        with st.container(border=True):
            label = "예측 고장" if f["label"] == "예측 고장" else "사람 확인"
            st.markdown(f"**설비 #{f['machine_id']} · {f['comp_name']}** &nbsp; {ui.badge_html(label)}",
                        unsafe_allow_html=True)
            ui.finding_lines(f)  # 기본 3줄: 이상 / 1순위 원인 / 할 일 (작업자 작업 카드와 같은 부품)
            with st.expander("자세히 보기", expanded=False):
                ui.finding_details(f)

        with st.container(border=True):
            st.subheader("센서 추이")
            try:
                tr = f["trend"] if MOCK else S.cached_trend(f["machine_id"], f["sensor"], S.as_of_text())
                ui.sensor_chart(f, tr, S.as_of())
            except Exception as e:
                st.error(f"센서 차트를 그리지 못했습니다: {e}")

        with st.container(border=True):
            st.subheader("엔지니어 피드백")
            st.caption("현장 확인 결과를 남기면 다음 조사와 채팅 답변에 반영됩니다.")
            fb = st.text_area("피드백", key=f"fb_{S.as_of():%Y%m%d}_{f['machine_id']}", label_visibility="collapsed",
                              placeholder="예: 압력 센서 고정 볼트 풀림이 원인이었음")
            if st.button("저장", key=f"fb_save_{f['machine_id']}", disabled=not fb.strip()):
                try:
                    if not MOCK:
                        S.svc().save_feedback(f["machine_id"], S.as_of_text(), fb, S.user_id() or "eng01")
                    ss.chat.append({"role": "assistant",
                                    "content": f"#{f['machine_id']} 엔지니어 피드백을 저장했습니다: \"{fb.strip()}\"\n\n"
                                               "채팅에서 이 설비를 물으면 피드백을 확인해 답에 반영합니다.",
                                    "route": "경로: 설비별 조사 → 장기 기억 저장 (get_engineer_feedback로 조회)",
                                    "elapsed": 0.0})
                    st.success(f"#{f['machine_id']} 피드백을 저장했습니다.")
                except Exception as e:
                    st.error(f"피드백 저장 실패: {e}")

# ── 승인 ────────────────────────────────────────────────────
def approve_group(keys: list[str]) -> None:
    """[이 팀 모두 승인]: 위젯이 그려지기 전에(콜백) 그 구역 카드들의 결정을 "승인"으로."""
    for k in keys:
        ss[k] = "승인"


def group_header(name: str, n: int, undecided: int = 0) -> None:
    extra = f" &nbsp; {ui.badge_html(f'미결정 {undecided}', 'orange')}" if undecided else ""
    st.markdown(f"**{html.escape(name)} · {n}건**{extra}", unsafe_allow_html=True)


def decision_key(draft_id: str) -> str:
    return f"dec_{ss.thread_id}_{draft_id}"


with tab3:
    res = ss.result
    requests = S.pending_requests()
    drafts = res["drafts"] if res else []
    drafts_by_id = {d["id"]: d for d in drafts}

    with st.container(border=True):
        st.subheader("승인 대기")
        if res is None:
            st.info("점검을 먼저 실행하세요.")
        elif requests:
            st.caption("카드마다 승인 또는 거절을 고른 뒤 [결정 제출]을 누르세요. 승인한 초안만 등록됩니다.")
            cards = [drafts_by_id.get(r["draft_id"], {"id": r["draft_id"], "type": "초안", "title": "초안",
                                                      "body": "", "group": "미배정"}) for r in requests]
            chosen: dict[str, str | None] = {}  # draft_id -> 승인/거절/None (화면 순서와 무관하게 id로 찾는다)
            for row in ui.group_sections(cards):
                for col, (name, group) in zip(st.columns(len(row)), row):
                    is_team = name not in ui.NON_TEAM_GROUPS
                    with col, st.container(border=True):
                        keys = [decision_key(d["id"]) for d in group]
                        group_header(name, len(group), sum(ss.get(k) is None for k in keys))
                        st.button("이 팀 모두 승인" if is_team else "이 구역 모두 승인", key=f"approve_all_{name}",
                                  on_click=approve_group, args=(keys,), type="tertiary")
                        for d in group:
                            chosen[d["id"]] = ui.approval_card(d, key=decision_key(d["id"]), hide_team=is_team)
            choices = [chosen.get(r["draft_id"]) for r in requests]  # 제출은 requests 원래 순서 그대로
            s1, s2 = st.columns([4, 1])
            s1.markdown(f"승인 {choices.count('승인')} · 거절 {choices.count('거절')} · 미결정 {choices.count(None)}")
            if s2.button("결정 제출", type="primary", key="submit_decisions", disabled=None in choices, width="stretch"):
                if MOCK:
                    for r, c in zip(requests, choices):
                        ss.decisions[r["draft_id"]] = c
                        S.log("Supervisor", f"결정 {'approve' if c == '승인' else 'reject'} → {r['draft_id']}")
                    st.rerun()
                else:
                    try:
                        with st.spinner("결정 반영 중 (승인분 등록 → 리포트 작성)"):
                            out = S.svc().submit_decisions(ss.thread_id, S.as_of_text(),
                                                           ["approve" if c == "승인" else "reject" for c in choices],
                                                           decided_by=S.user_id())
                        ss.pending = out.get("requests") if out["status"] == "pending" else None
                        ss.last_answer = out.get("answer")
                        S.refresh_from_services()
                        st.rerun()
                    except Exception as e:
                        st.error(f"결정 제출 실패: {type(e).__name__}: {e}")
        elif not [d for d in drafts if d.get("status") != "SUPERSEDED"]:
            st.success("승인할 초안이 없습니다.")
        else:
            for row in ui.group_sections([d for d in drafts if d.get("status") != "SUPERSEDED"]):
                for col, (name, group) in zip(st.columns(len(row)), row):
                    with col, st.container(border=True):
                        group_header(name, len(group))
                        for d in group:
                            status = ss.decisions.get(d["id"], "대기") if MOCK else ui.status_text(d["status"])
                            ui.decided_card(d, status, hide_team=name not in ui.NON_TEAM_GROUPS)
            registered = (sum(v == "승인" for v in ss.decisions.values()) if MOCK
                          else sum(d["status"] == "APPROVED" for d in drafts))
            st.caption(f"등록 {registered}건 · 승인 없이 등록된 초안은 없습니다.")
        superseded = [d for d in drafts if d.get("status") == "SUPERSEDED"]
        if superseded:
            with st.expander(f"대체된 초안 {len(superseded)}건 (이전 배정안)"):
                for d in superseded:
                    line = " ".join(x for x in (d.get("machine") or d["title"], d.get("task"), d.get("when")) if x)
                    st.markdown(f"<span class='muted'>[{html.escape(d['type'])}] {html.escape(line)}</span> "
                                f"{ui.badge_html('대체됨')}", unsafe_allow_html=True)
        if ss.last_answer and not MOCK and not requests:
            st.info(ss.last_answer)

    # ── 재계획 (거절 사유 반영) ──
    if res is not None and res["findings"]:
        with st.container(border=True):
            st.subheader("재계획 요청")
            st.caption("배정안을 그대로 승인할 수 없을 때 사유를 적으면 조건을 반영해 다시 배정합니다.")
            if MOCK:
                st.caption("목업 모드에서는 재계획을 지원하지 않습니다.")
            else:
                reason = st.text_input("재계획 요청 사유", key=f"replan_reason_{ss.thread_id}",
                                       placeholder="예: 2팀 오늘 교육이라 불가 / 가장 위험한 설비는 생산 일정 때문에 오늘 정비 불가")
                if st.button("재계획 요청", key="replan_btn", disabled=not reason.strip() or S.job_running()):
                    try:
                        with st.spinner("사유 해석 → 재배정 → 새 초안 작성 중"):
                            out = S.svc().request_replan(ss.thread_id, S.as_of_text(), reason,
                                                         decided_by=S.user_id())
                        ss.pending = out.get("requests") if out["status"] == "pending" else None
                        ss.last_answer = out.get("answer")
                        S.refresh_from_services()
                        st.rerun()
                    except Exception as e:
                        st.error(f"재계획 실패: {type(e).__name__}: {e}")
            diff = res.get("diff")
            if diff is not None and not diff.empty:
                st.markdown(f"**이전 배정과 새 배정 비교 (배정안 v{res['plan_version']})** · 바뀐 행 강조")
                st.dataframe(diff.style.apply(lambda row: [CHANGED_STYLE if row["변경"] else ""] * len(row), axis=1),
                             hide_index=True, width="stretch")

    # 인수인계 리포트는 이력·보고서 페이지에서만 본다 (승인 결정 후 자동 작성·저장은 그대로)
    if res is not None and not requests and not MOCK:
        st.caption("인수인계 리포트는 승인 결정이 끝나면 자동으로 저장되며, 이력·보고서 페이지에서 볼 수 있습니다.")


# ── 채팅: 오른쪽 아래 아이콘 → 대화창 (모든 탭에서) ─────────────
def mock_answer(q: str, res: dict | None) -> tuple[str, str]:
    """목업 채팅 답변과 경로 (LLM 없음)."""
    ql = q.replace(" ", "")
    nums = [int(n) for n in re.findall(r"(\d+)번", q)]
    dates = re.findall(r"(\d{1,2})월\s*(\d{1,2})일", q)
    finding = None
    if res and nums:
        finding = next((x for x in res["findings"] if x["machine_id"] == nums[0]), None)
    if any(w in ql for w in ["정지", "재시작", "설정변경", "꺼", "멈춰"]):
        return "설비 직접 제어 요청은 처리할 수 없습니다. 이 에이전트는 조회와 초안 작성만 합니다.", BLOCKED_ROUTE
    if dates and date(2015, int(dates[0][0]), int(dates[0][1])) > ss.day:
        return (f"{dates[0][0]}월 {dates[0][1]}일은 기준 시각({S.as_of():%m/%d}) 이후라 조회할 수 없습니다.",
                "Data Agent → run_sql_guarded: WHERE datetime <= as_of → 0행")
    if any(w in ql for w in ["몇건", "건수", "몇대", "평균", "합계"]):
        n = int(mock.rng_for(S.as_of(), len(q)).integers(8, 30))
        return (f"기준 시각 이전 30일 동안 해당 조건의 고장은 {n}건입니다. (가짜 값)",
                "Supervisor → ask_data_agent → sql_db_schema → run_sql_guarded")
    if finding:
        plan = res["plan"]
        asg = plan.loc[plan.machine_id == finding["machine_id"], "slot"].iloc[0]
        return (f"#{finding['machine_id']}은 {asg}입니다. {finding['summary']} (근거 번호 {finding['evidence_id']})",
                "Supervisor가 근거 번호로 답변 — 추가 도구 호출 없음")
    if nums:
        return f"#{nums[0]}은 오늘 고위험 목록에 없습니다.", "Supervisor → 고위험 목록 확인"
    if any(w in ql for w in ["점검방법", "매뉴얼", "절차", "어떻게"]):
        key = next((c for c, v in mock.COMP.items() if v[0].split("/")[0] in q), "comp4")
        return f"{mock.MANUAL[key][1]}\n\n[출처: {mock.MANUAL[key][0]}]", "Supervisor → Investigator(search_manual)"
    return "오늘 점검 결과, 설비별 근거, 고장 통계, 점검 절차를 물어볼 수 있습니다.", "Supervisor 직접 응답"


def open_chat() -> None:
    ss.chat_open = True


def close_chat() -> None:
    ss.chat_open = False


@st.dialog("채팅", width="large", on_dismiss=close_chat)
def chat_dialog() -> None:
    """채팅 대화창. 답변 후에는 대화창만 다시 그린다 (닫히지 않게).
    답변으로 새 승인 대기가 생기면 대화창을 닫고 전체를 다시 그려 승인 카드가 보이게 한다."""
    res = ss.result
    requests = S.pending_requests()
    ex = (f"{res['findings'][0]['machine_id']}번은 왜 이렇게 배정됐어?" if res and res["findings"]
          else "오늘 위험한 설비 있어?")
    c1, c2 = st.columns([5, 1])
    c1.caption(f"예: {ex} · 10월에 베어링 고장 몇 건이야? · 베어링 점검 방법")
    if c2.button("대화 지우기", key="chat_clear", type="tertiary", disabled=not ss.chat):
        ss.chat = []
        ui.rerun_fragment()
    chat_blocked = bool(requests) or S.job_running()
    if requests:
        st.warning("승인 대기를 먼저 처리하세요. 처리 전에는 채팅을 보낼 수 없습니다.")
    box = st.container(height=420, border=False)
    for m in ss.chat:
        with box.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("route") and S.dev_visible():
                st.caption(m["route"])
    q = st.chat_input("질문을 입력하세요", disabled=chat_blocked, key="chat_input")
    if not q or chat_blocked:
        return
    ss.chat.append({"role": "user", "content": q})
    t0 = time.perf_counter()
    if MOCK:
        ans, route = mock_answer(q, res)
        ss.chat.append({"role": "assistant", "content": ans, "route": f"경로: {route}",
                        "elapsed": time.perf_counter() - t0})
        S.log("Supervisor", f"채팅 처리 — {route}")
        ui.rerun_fragment()
    try:
        with st.spinner("답변을 준비하고 있습니다"):
            out = S.svc().chat(ss.thread_id, S.as_of_text(), q)
        if out["blocked"]:
            route = BLOCKED_ROUTE
        elif out["used_tools"]:
            route = "Supervisor → " + " → ".join(out["used_tools"])
        else:
            route = "Supervisor 직접 응답 (도구 호출 없음)"
        ss.chat.append({"role": "assistant", "content": out["answer"], "route": f"경로: {route}",
                        "elapsed": time.perf_counter() - t0})
        if ss.result is not None:
            ss.result = S.svc().load_day(S.as_of_text())
        ss.trace = S.trace_from_events(S.svc().get_job(ss.thread_id)["events"])
        new_pending = out["status"] == "pending"
        if new_pending:
            ss.pending = out["requests"]
    except Exception as e:
        ss.chat.append({"role": "assistant", "content": f"오류가 발생했습니다: {type(e).__name__}: {e}",
                        "elapsed": time.perf_counter() - t0})
        new_pending = False
    if new_pending:  # 새 승인 대기 -> 대화창을 닫고 화면 전체를 다시 그려 승인 카드가 보이게
        ss.chat_open = False
        st.rerun()
    ui.rerun_fragment()


ss.setdefault("chat_open", False)
n_msgs, has_pending = len(ss.chat), bool(S.pending_requests())
st.button("", icon=":material/chat:", key="chat_fab", help="채팅", on_click=open_chat)
if n_msgs or has_pending:  # 아이콘 위 작은 표시: 메시지 수 / 승인 대기 주황 점
    st.markdown((f'<div class="chat-fab-count">{n_msgs}</div>' if n_msgs else "")
                + ('<div class="chat-fab-dot" title="승인 대기"></div>' if has_pending else ""),
                unsafe_allow_html=True)
if ss.chat_open:
    chat_dialog()

# ──────────────────────────────────────────────────────────────
# 개발자 로그 (설정에서 개발자 모드 ON + 계정 allow_dev)
# ──────────────────────────────────────────────────────────────
if S.dev_visible():
    with st.expander("개발자 로그", expanded=False):
        d1, d2, d3 = st.tabs(["에이전트 실행 로그", "설비별 도구 호출 기록", "채팅 경로·소요 시간"])
        with d1:
            if ss.trace:
                st.code("\n".join(ss.trace), language=None)
            else:
                st.caption("실행 기록이 없습니다.")
        with d2:
            res = ss.result
            if res is None or not res["findings"]:
                st.caption("조사 기록이 없습니다.")
            for f in (res or {}).get("findings", []):
                st.markdown(f"**#{f['machine_id']} {f['comp_name']}** · 도구 호출 {len(f['tool_calls'])}/{MAX_TOOL_CALLS}회")
                if gc := f.get("grounding_check"):
                    st.caption(
                        f"grounding_check: 처음 문제 {len(gc['problems'])}건, 고쳐 쓰기 {'예' if gc['rewritten'] else '아니오'},"
                        f" 남은 문제 {len(gc['after_rewrite'])}건, 뺀 항목 {len(gc['removed'])}건,"
                        f" 안전 단계 삽입 {'예' if gc['safety_inserted'] else '아니오'}")
                    if gc["problems"]:
                        lines = ["[처음]", *gc["problems"]]
                        if gc["after_rewrite"]:
                            lines += ["[고쳐 쓴 뒤]", *gc["after_rewrite"]]
                        lines += [f"[뺀 항목] {r['field']}: {json.dumps(r['item'], ensure_ascii=False)}"
                                  for r in gc["removed"]]
                        st.code("\n".join(lines), language=None)
                st.dataframe(pd.DataFrame(f["tool_calls"], columns=["도구", "입력", "결과"]).assign(
                    결과=lambda df: df["결과"].str.slice(0, 300)), hide_index=True, width="stretch")
        with d3:
            rows = []
            question = None
            for m in ss.chat:
                if m["role"] == "user":
                    question = m["content"]
                elif m.get("route"):
                    rows.append({"질문": question or "-", "경로": m["route"].removeprefix("경로: "),
                                 "소요 시간(초)": round(m.get("elapsed", 0.0), 2)})
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                             column_config={"소요 시간(초)": st.column_config.NumberColumn(format="%.2f")})
            else:
                st.caption("채팅 기록이 없습니다.")
